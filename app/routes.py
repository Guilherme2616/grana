import calendar
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import extract, func
from werkzeug.utils import secure_filename

from .extensions import db
from .models import Account, CardCycle, Category, CreditCard, Investment, Invoice, InvoiceItem, Transaction, User
from .services.invoice_parser import PdfPasswordInvalid, PdfPasswordRequired, parse_invoice_pdf


main = Blueprint("main", __name__)


def money(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def form_decimal(name, default="0"):
    raw = request.form.get(name, default).strip().replace("R$", "").replace(".", "").replace(",", ".")
    return Decimal(raw)


def safe_date(year, month, day):
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_day))


def default_cycle_dates(card, reference_month, due_date=None):
    reference = datetime.strptime(reference_month, "%Y-%m")
    due = due_date or safe_date(reference.year, reference.month, card.due_day)
    closing_year, closing_month = due.year, due.month
    if card.closing_day >= due.day:
        closing_month -= 1
        if closing_month == 0:
            closing_month, closing_year = 12, closing_year - 1
    return safe_date(closing_year, closing_month, card.closing_day), due


def upsert_card_cycle(card, reference_month, closing_date, due_date, source="manual"):
    cycle = CardCycle.query.filter_by(card_id=card.id, reference_month=reference_month).first()
    if not cycle:
        cycle = CardCycle(card_id=card.id, reference_month=reference_month)
        db.session.add(cycle)
    cycle.closing_date = closing_date
    cycle.due_date = due_date
    cycle.source = source
    return cycle


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


@main.route("/cartoes/<int:card_id>/configurar", methods=["GET", "POST"])
@login_required
def configure_card(card_id):
    card = db.get_or_404(CreditCard, card_id)
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "card":
                card.name = request.form["name"].strip()
                card.last_digits = request.form.get("last_digits", "")[-4:]
                card.credit_limit = form_decimal("credit_limit")
                card.closing_day = int(request.form["closing_day"])
                card.due_day = int(request.form["due_day"])
                card.color = request.form.get("color", "#173F35")
                if not 1 <= card.closing_day <= 31 or not 1 <= card.due_day <= 31:
                    raise ValueError
                flash("Configuração padrão do cartão atualizada.", "success")
            elif action == "cycle":
                reference_month = request.form["reference_month"]
                datetime.strptime(reference_month, "%Y-%m")
                closing_date = datetime.strptime(request.form["closing_date"], "%Y-%m-%d").date()
                due_date = datetime.strptime(request.form["due_date"], "%Y-%m-%d").date()
                if closing_date >= due_date:
                    raise ValueError
                upsert_card_cycle(card, reference_month, closing_date, due_date)
                flash("Datas específicas dessa fatura salvas.", "success")
            db.session.commit()
            return redirect(url_for("main.configure_card", card_id=card.id))
        except (ValueError, InvalidOperation):
            db.session.rollback()
            flash("Confira as datas e os valores informados.", "danger")

    month = request.args.get("month") or date.today().strftime("%Y-%m")
    try:
        default_closing, default_due = default_cycle_dates(card, month)
    except ValueError:
        month = date.today().strftime("%Y-%m")
        default_closing, default_due = default_cycle_dates(card, month)
    cycles = CardCycle.query.filter_by(card_id=card.id).order_by(CardCycle.reference_month.desc()).all()
    return render_template("configure_card.html", card=card, cycles=cycles, month=month, default_closing=default_closing, default_due=default_due)


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
            parsed = parse_invoice_pdf(file.stream, reference_month, request.form.get("pdf_password", ""))
            if not parsed["items"]:
                flash("Não consegui localizar compras automaticamente nesse PDF.", "warning"); return render_template("import_invoice.html", cards=cards)
            card = db.get_or_404(CreditCard, int(request.form["card_id"]))
            invoice = Invoice(card_id=card.id, reference_month=reference_month, original_filename=secure_filename(file.filename), status="draft")
            db.session.add(invoice); db.session.flush()
            for item in parsed["items"]: db.session.add(InvoiceItem(invoice_id=invoice.id, **item))
            invoice.total = sum((item["amount"] for item in parsed["items"]), Decimal("0")); db.session.commit()
            closing_date, due_date = default_cycle_dates(card, reference_month, parsed["due_date"])
            session[f"invoice_cycle_{invoice.id}"] = {
                "closing_date": closing_date.isoformat(),
                "due_date": due_date.isoformat(),
                "source": "pdf" if parsed["due_date"] else "default",
            }
            return redirect(url_for("main.review_invoice", invoice_id=invoice.id))
        except PdfPasswordRequired:
            db.session.rollback(); flash("Este PDF tem senha. Informe-a no campo Senha do PDF.", "warning")
        except PdfPasswordInvalid:
            db.session.rollback(); flash("A senha informada para o PDF está incorreta.", "danger")
        except Exception:
            db.session.rollback(); flash("Não foi possível ler o PDF. Tente outro arquivo.", "danger")
    return render_template("import_invoice.html", cards=cards)


@main.route("/faturas/<int:invoice_id>/revisar", methods=["GET", "POST"])
@login_required
def review_invoice(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    if invoice.status != "draft": return redirect(url_for("main.dashboard"))
    categories = Category.query.filter_by(kind="expense").order_by(Category.name).all()
    suggestion = session.get(f"invoice_cycle_{invoice.id}")
    if not suggestion:
        closing_date, due_date = default_cycle_dates(invoice.card, invoice.reference_month)
        suggestion = {"closing_date": closing_date.isoformat(), "due_date": due_date.isoformat(), "source": "default"}
    if request.method == "POST":
        selected_ids = {int(value) for value in request.form.getlist("selected")}
        closing_date = datetime.strptime(request.form["closing_date"], "%Y-%m-%d").date()
        due_date = datetime.strptime(request.form["due_date"], "%Y-%m-%d").date()
        if closing_date >= due_date:
            flash("O fechamento precisa acontecer antes do vencimento.", "danger")
            return render_template("review_invoice.html", invoice=invoice, categories=categories, suggestion=suggestion)
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
        source = "pdf" if request.form.get("date_source") == "pdf" else "manual"
        upsert_card_cycle(invoice.card, invoice.reference_month, closing_date, due_date, source)
        invoice.total = total; invoice.status = "confirmed"; db.session.commit()
        session.pop(f"invoice_cycle_{invoice.id}", None)
        flash(f"Fatura importada com {len(selected_ids)} compras e datas atualizadas.", "success")
        return redirect(url_for("main.dashboard"))
    return render_template("review_invoice.html", invoice=invoice, categories=categories, suggestion=suggestion)


@main.route("/investimentos", methods=["GET", "POST"])
@login_required
def investments():
    if request.method == "POST":
        try:
            item = Investment(
                operation=request.form["operation"], category=request.form["category"].strip(),
                subcategory=request.form.get("subcategory", "").strip(), asset=request.form["asset"].strip().upper(),
                quantity=form_decimal("quantity"), unit_value=form_decimal("unit_value"),
                operation_date=datetime.strptime(request.form["operation_date"], "%Y-%m-%d").date(),
                notes=request.form.get("notes", "").strip(),
            )
            if item.operation not in {"Compra", "Venda", "Recebimento"} or not item.category or not item.asset or item.quantity <= 0 or item.unit_value < 0:
                raise ValueError
            db.session.add(item); db.session.commit(); flash("Investimento registrado.", "success")
            return redirect(url_for("main.investments"))
        except (ValueError, InvalidOperation):
            db.session.rollback(); flash("Confira os dados do investimento.", "danger")

    query = Investment.query
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    category = request.args.get("category", "")
    subcategory = request.args.get("subcategory", "")
    if start:
        try: query = query.filter(Investment.operation_date >= datetime.strptime(start, "%Y-%m-%d").date())
        except ValueError: pass
    if end:
        try: query = query.filter(Investment.operation_date <= datetime.strptime(end, "%Y-%m-%d").date())
        except ValueError: pass
    if category: query = query.filter_by(category=category)
    if subcategory: query = query.filter_by(subcategory=subcategory)
    items = query.order_by(Investment.operation_date.desc(), Investment.id.desc()).all()

    all_items = Investment.query.all()
    total_buys = sum((item.total_value for item in all_items if item.operation == "Compra"), Decimal("0"))
    total_sales = sum((item.total_value for item in all_items if item.operation == "Venda"), Decimal("0"))
    total_receipts = sum((item.total_value for item in all_items if item.operation == "Recebimento"), Decimal("0"))
    investment_categories = Category.query.filter(func.lower(Category.name).in_(["investimento", "investimentos"])).all()
    category_ids = [item.id for item in investment_categories]
    total_transferred = money(db.session.query(func.sum(Transaction.amount)).filter(Transaction.kind == "expense", Transaction.category_id.in_(category_ids)).scalar()) if category_ids else Decimal("0")
    invested_value = total_buys - total_sales
    broker_balance = total_transferred - total_buys + total_sales + total_receipts
    categories = [row[0] for row in db.session.query(Investment.category).distinct().order_by(Investment.category).all()]
    subcategories = [row[0] for row in db.session.query(Investment.subcategory).filter(Investment.subcategory != "").distinct().order_by(Investment.subcategory).all()]
    return render_template("investments.html", investments=items, today=date.today(), total_transferred=total_transferred, total_buys=total_buys, total_sales=total_sales, total_receipts=total_receipts, invested_value=invested_value, broker_balance=broker_balance, categories=categories, subcategories=subcategories, filters={"start":start,"end":end,"category":category,"subcategory":subcategory})


@main.route("/investimentos/<int:item_id>/editar", methods=["GET", "POST"])
@login_required
def edit_investment(item_id):
    item = db.get_or_404(Investment, item_id)
    if request.method == "POST":
        try:
            item.operation = request.form["operation"]
            item.category = request.form["category"].strip()
            item.subcategory = request.form.get("subcategory", "").strip()
            item.asset = request.form["asset"].strip().upper()
            item.quantity = form_decimal("quantity")
            item.unit_value = form_decimal("unit_value")
            item.operation_date = datetime.strptime(request.form["operation_date"], "%Y-%m-%d").date()
            item.notes = request.form.get("notes", "").strip()
            if item.operation not in {"Compra", "Venda", "Recebimento"} or not item.category or not item.asset or item.quantity <= 0 or item.unit_value < 0:
                raise ValueError
            db.session.commit(); flash("Investimento atualizado.", "success")
            return redirect(url_for("main.investments"))
        except (ValueError, InvalidOperation):
            db.session.rollback(); flash("Confira os dados informados.", "danger")
    return render_template("edit_investment.html", item=item)


@main.post("/investimentos/<int:item_id>/excluir")
@login_required
def delete_investment(item_id):
    item = db.get_or_404(Investment, item_id)
    db.session.delete(item); db.session.commit(); flash("Registro de investimento excluído.", "success")
    return redirect(url_for("main.investments"))
