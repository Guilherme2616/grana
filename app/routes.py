from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import extract, func
from werkzeug.utils import secure_filename

from .extensions import db
from .models import Account, Category, CreditCard, Invoice, InvoiceItem, Transaction, User
from .services.invoice_parser import parse_invoice_pdf


main = Blueprint("main", __name__)


def money(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def form_decimal(name, default="0"):
    raw = request.form.get(name, default).strip().replace("R$", "").replace(".", "").replace(",", ".")
    return Decimal(raw)


@main.app_template_filter("brl")
def brl(value):
    value = money(value)
    formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatted}"


@main.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        user = User.query.filter_by(username=request.form.get("username", "").strip()).first()
        if user and user.check_password(request.form.get("password", "")):
            login_user(user, remember=bool(request.form.get("remember")))
            return redirect(url_for("main.dashboard"))
        flash("Usuário ou senha incorretos.", "danger")
    return render_template("login.html")


@main.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("main.login"))


@main.get("/")
@login_required
def dashboard():
    today = date.today()
    base = Transaction.query.filter(extract("month", Transaction.transaction_date) == today.month, extract("year", Transaction.transaction_date) == today.year)
    income = money(base.filter_by(kind="income").with_entities(func.sum(Transaction.amount)).scalar())
    expenses = money(base.filter_by(kind="expense").with_entities(func.sum(Transaction.amount)).scalar())
    initial = money(db.session.query(func.sum(Account.initial_balance)).filter(Account.active.is_(True)).scalar())
    all_income = money(db.session.query(func.sum(Transaction.amount)).filter_by(kind="income").scalar())
    all_expenses = money(db.session.query(func.sum(Transaction.amount)).filter_by(kind="expense").scalar())
    balance = initial + all_income - all_expenses
    next_invoice = Invoice.query.filter_by(status="confirmed").order_by(Invoice.reference_month.desc()).first()
    transactions = Transaction.query.order_by(Transaction.transaction_date.desc(), Transaction.id.desc()).limit(5).all()
    return render_template("dashboard.html", balance=balance, income=income, expenses=expenses, next_invoice=next_invoice, transactions=transactions, today=today)


@main.route("/movimentacoes", methods=["GET", "POST"])
@login_required
def transactions():
    if request.method == "POST":
        try:
            transaction = Transaction(
                description=request.form["description"].strip(), amount=form_decimal("amount"),
                kind=request.form["kind"], transaction_date=datetime.strptime(request.form["transaction_date"], "%Y-%m-%d").date(),
                account_id=request.form.get("account_id") or None, category_id=request.form.get("category_id") or None,
                card_id=request.form.get("card_id") or None, notes=request.form.get("notes", "").strip(),
            )
            if not transaction.description or transaction.amount <= 0:
                raise ValueError
            db.session.add(transaction); db.session.commit(); flash("Lançamento salvo.", "success")
            return redirect(url_for("main.transactions"))
        except (ValueError, InvalidOperation):
            db.session.rollback(); flash("Confira a descrição, o valor e a data.", "danger")
    items = Transaction.query.order_by(Transaction.transaction_date.desc(), Transaction.id.desc()).all()
    return render_template("transactions.html", transactions=items, accounts=Account.query.filter_by(active=True).all(), categories=Category.query.order_by(Category.name).all(), cards=CreditCard.query.filter_by(active=True).all(), today=date.today())


@main.post("/movimentacoes/<int:item_id>/excluir")
@login_required
def delete_transaction(item_id):
    item = db.get_or_404(Transaction, item_id); db.session.delete(item); db.session.commit(); flash("Lançamento excluído.", "success")
    return redirect(url_for("main.transactions"))


@main.route("/contas", methods=["GET", "POST"])
@login_required
def accounts():
    if request.method == "POST":
        try:
            db.session.add(Account(name=request.form["name"].strip(), institution=request.form.get("institution", "").strip(), account_type=request.form.get("account_type", "corrente"), initial_balance=form_decimal("initial_balance")))
            db.session.commit(); flash("Conta adicionada.", "success"); return redirect(url_for("main.accounts"))
        except (ValueError, InvalidOperation):
            db.session.rollback(); flash("Confira os dados da conta.", "danger")
    return render_template("accounts.html", accounts=Account.query.order_by(Account.name).all())


@main.route("/categorias", methods=["GET", "POST"])
@login_required
def categories():
    if request.method == "POST":
        name = request.form["name"].strip()
        if name:
            db.session.add(Category(name=name, kind=request.form.get("kind", "expense"), color=request.form.get("color", "#D8B56A"), icon=request.form.get("icon", "$")))
            db.session.commit(); flash("Categoria adicionada.", "success"); return redirect(url_for("main.categories"))
    return render_template("categories.html", categories=Category.query.order_by(Category.kind, Category.name).all())


@main.route("/cartoes", methods=["GET", "POST"])
@login_required
def cards():
    if request.method == "POST":
        try:
            card = CreditCard(name=request.form["name"].strip(), last_digits=request.form.get("last_digits", "")[-4:], credit_limit=form_decimal("credit_limit"), closing_day=int(request.form["closing_day"]), due_day=int(request.form["due_day"]), color=request.form.get("color", "#173F35"))
            if not 1 <= card.closing_day <= 31 or not 1 <= card.due_day <= 31: raise ValueError
            db.session.add(card); db.session.commit(); flash("Cartão adicionado.", "success"); return redirect(url_for("main.cards"))
        except (ValueError, InvalidOperation):
            db.session.rollback(); flash("Confira os dados do cartão.", "danger")
    return render_template("cards.html", cards=CreditCard.query.order_by(CreditCard.name).all())


@main.route("/faturas/importar", methods=["GET", "POST"])
@login_required
def import_invoice():
    cards = CreditCard.query.filter_by(active=True).all()
    if request.method == "POST":
        file = request.files.get("invoice")
        if not file or not file.filename or not file.filename.lower().endswith(".pdf"):
            flash("Selecione uma fatura em PDF.", "danger"); return render_template("import_invoice.html", cards=cards)
        try:
            reference_month = request.form["reference_month"]
            datetime.strptime(reference_month, "%Y-%m")
            parsed = parse_invoice_pdf(file.stream, reference_month)
            if not parsed:
                flash("Não consegui localizar compras automaticamente nesse PDF.", "warning"); return render_template("import_invoice.html", cards=cards)
            invoice = Invoice(card_id=int(request.form["card_id"]), reference_month=reference_month, original_filename=secure_filename(file.filename), status="draft")
            db.session.add(invoice); db.session.flush()
            for item in parsed: db.session.add(InvoiceItem(invoice_id=invoice.id, **item))
            invoice.total = sum((item["amount"] for item in parsed), Decimal("0")); db.session.commit()
            return redirect(url_for("main.review_invoice", invoice_id=invoice.id))
        except Exception:
            db.session.rollback(); flash("Não foi possível ler o PDF. Tente outro arquivo.", "danger")
    return render_template("import_invoice.html", cards=cards)


@main.route("/faturas/<int:invoice_id>/revisar", methods=["GET", "POST"])
@login_required
def review_invoice(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    if invoice.status != "draft": return redirect(url_for("main.dashboard"))
    categories = Category.query.filter_by(kind="expense").order_by(Category.name).all()
    if request.method == "POST":
        selected_ids = {int(value) for value in request.form.getlist("selected")}
        total = Decimal("0")
        for item in invoice.items:
            item.selected = item.id in selected_ids
            item.description = request.form.get(f"description_{item.id}", item.description).strip()[:180]
            try: item.amount = Decimal(request.form.get(f"amount_{item.id}", str(item.amount)).replace(".", "").replace(",", "."))
            except InvalidOperation: pass
            item.category_id = request.form.get(f"category_{item.id}") or None
            if item.selected:
                total += item.amount
                db.session.add(Transaction(description=item.description, amount=item.amount, kind="expense", transaction_date=item.purchase_date, card_id=invoice.card_id, category_id=item.category_id, invoice_item_id=item.id))
        invoice.total = total; invoice.status = "confirmed"; db.session.commit(); flash(f"Fatura importada com {len(selected_ids)} compras.", "success")
        return redirect(url_for("main.dashboard"))
    return render_template("review_invoice.html", invoice=invoice, categories=categories)

