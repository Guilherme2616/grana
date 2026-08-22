import calendar
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import extract, func
from werkzeug.utils import secure_filename

from .extensions import db
from .models import Account, CardCycle, Category, CreditCard, Investment, Invoice, InvoiceItem, Transaction, User
from .services.drive_sync import (
    DriveAccessError,
    DriveConfigurationError,
    download_pdf,
    drive_is_configured,
    list_month_pdfs,
)
from .services.financial_analytics import (
    build_installment_projection,
    month_bounds,
    month_label,
    shift_month,
)
from .services.invoice_parser import PdfPasswordInvalid, PdfPasswordRequired, parse_invoice_pdf
from .services.secret_store import decrypt_secret, encrypt_secret


main = Blueprint("main", __name__)

INVOICE_PROVIDERS = (
    ("", "Selecione o banco"),
    ("bb_smiles", "Banco do Brasil • Smiles"),
    ("mercado_pago", "Mercado Pago"),
    ("banco_inter", "Banco Inter"),
    ("itau", "Itaú"),
)
PROVIDER_LABELS = dict(INVOICE_PROVIDERS)


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


def update_card_pdf_settings(card):
    provider = request.form.get("invoice_provider", "")
    if provider not in PROVIDER_LABELS:
        raise ValueError
    card.invoice_provider = provider
    if request.form.get("clear_pdf_password"):
        card.pdf_password_encrypted = None
    elif request.form.get("pdf_password"):
        card.pdf_password_encrypted = encrypt_secret(request.form["pdf_password"])


def create_invoice_draft(card, parsed, filename, drive_file_id=None):
    reference_month = parsed["reference_month"]
    invoice = Invoice(
        card_id=card.id,
        reference_month=reference_month,
        original_filename=secure_filename(filename),
        status="draft",
        source=parsed.get("adapter", "generic"),
        credit_limit=parsed.get("credit_limit"),
        cash_advance_total=parsed.get("cash_advance_total"),
        drive_file_id=drive_file_id,
    )
    db.session.add(invoice)
    db.session.flush()
    for item in parsed["items"]:
        db.session.add(InvoiceItem(invoice_id=invoice.id, **item))
    parsed_items_total = sum((item["amount"] for item in parsed["items"]), Decimal("0"))
    invoice.total = parsed.get("statement_total") or parsed_items_total
    closing_date, due_date = default_cycle_dates(card, reference_month, parsed["due_date"])
    session[f"invoice_cycle_{invoice.id}"] = {
        "closing_date": closing_date.isoformat(),
        "due_date": due_date.isoformat(),
        "source": "pdf" if parsed["due_date"] else "default",
        "statement_total": str(parsed.get("statement_total") or ""),
        "adapter": parsed.get("adapter", "generic"),
        "credit_limit": str(parsed.get("credit_limit") or ""),
        "cash_advance_total": str(parsed.get("cash_advance_total") or ""),
    }
    return invoice


def parse_with_saved_passwords(stream_factory, reference_month, cards):
    passwords = [""]
    for card in cards:
        password = decrypt_secret(card.pdf_password_encrypted)
        if password and password not in passwords:
            passwords.append(password)

    password_error = None
    for password in passwords:
        try:
            return parse_invoice_pdf(stream_factory(), reference_month, password)
        except (PdfPasswordRequired, PdfPasswordInvalid) as exc:
            password_error = exc
    if password_error:
        raise PdfPasswordRequired("Nenhuma senha cadastrada desbloqueou este PDF.")
    raise ValueError("Não foi possível interpretar o PDF.")


def percentage_change(current, previous):
    if not previous:
        return None
    return ((current - previous) / previous * Decimal("100")).quantize(Decimal("0.1"))


def investment_position():
    items = Investment.query.all()
    buys = sum((item.total_value for item in items if item.operation == "Compra"), Decimal("0"))
    sales = sum((item.total_value for item in items if item.operation == "Venda"), Decimal("0"))
    return buys - sales


def cash_balance():
    initial = money(db.session.query(func.sum(Account.initial_balance)).filter(Account.active.is_(True)).scalar())
    income = money(db.session.query(func.sum(Transaction.amount)).filter_by(kind="income").scalar())
    expenses = money(db.session.query(func.sum(Transaction.amount)).filter_by(kind="expense").scalar())
    return initial + income - expenses


def future_invoice_rows(base_month, months=12):
    last_month = shift_month(base_month, months)
    installment_items = (
        InvoiceItem.query.join(Invoice)
        .filter(
            Invoice.status == "confirmed",
            InvoiceItem.selected.is_(True),
            InvoiceItem.installment_total.isnot(None),
        ).all()
    )
    projection = build_installment_projection(installment_items, base_month, months)
    cards = {card.id: card for card in CreditCard.query.filter_by(active=True).all()}

    known = {}
    known_invoices = Invoice.query.filter(
        Invoice.status == "confirmed",
        Invoice.reference_month > base_month,
        Invoice.reference_month <= last_month,
    ).all()
    for invoice in known_invoices:
        key = (invoice.reference_month, invoice.card_id)
        known[key] = known.get(key, Decimal("0")) + Decimal(invoice.total)

    rows = []
    for offset in range(1, months + 1):
        reference = shift_month(base_month, offset)
        per_card = {}
        details = []
        for item in projection.get(reference, []):
            key = (reference, item["card_id"])
            if key in known:
                continue
            per_card[item["card_id"]] = per_card.get(item["card_id"], Decimal("0")) + item["amount"]
            details.append(item)
        for (known_month, card_id), amount in known.items():
            if known_month == reference:
                per_card[card_id] = amount
        card_values = [{
            "id": card_id,
            "name": cards[card_id].name if card_id in cards else "Cartão",
            "color": cards[card_id].color if card_id in cards else "#8E8D8A",
            "amount": amount,
            "confirmed": (reference, card_id) in known,
        } for card_id, amount in sorted(per_card.items(), key=lambda pair: pair[1], reverse=True)]
        rows.append({
            "reference_month": reference,
            "label": month_label(reference),
            "short_label": month_label(reference, short=True),
            "total": sum(per_card.values(), Decimal("0")),
            "cards": card_values,
            "details": details,
        })
    return rows


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


@main.get("/indicadores")
@login_required
def indicators():
    reference_month = request.args.get("month") or date.today().strftime("%Y-%m")
    try:
        start, end = month_bounds(reference_month)
    except (ValueError, IndexError):
        reference_month = date.today().strftime("%Y-%m")
        start, end = month_bounds(reference_month)

    previous_month = shift_month(reference_month, -1)
    previous_start, previous_end = month_bounds(previous_month)
    month_items = Transaction.query.filter(Transaction.transaction_date.between(start, end)).all()
    previous_items = Transaction.query.filter(Transaction.transaction_date.between(previous_start, previous_end)).all()
    income = sum((Decimal(item.amount) for item in month_items if item.kind == "income"), Decimal("0"))
    expenses = sum((Decimal(item.amount) for item in month_items if item.kind == "expense"), Decimal("0"))
    previous_income = sum((Decimal(item.amount) for item in previous_items if item.kind == "income"), Decimal("0"))
    previous_expenses = sum((Decimal(item.amount) for item in previous_items if item.kind == "expense"), Decimal("0"))
    net = income - expenses
    savings_rate = (net / income * Decimal("100")).quantize(Decimal("0.1")) if income else None

    category_data = {}
    card_data = {}
    origin_data = {"Cartões": Decimal("0"), "Contas e outros": Decimal("0")}
    daily_data = {day: Decimal("0") for day in range(1, end.day + 1)}
    expense_items = [item for item in month_items if item.kind == "expense"]
    for item in expense_items:
        category_name = item.category.name if item.category else "Sem categoria"
        category_color = item.category.color if item.category else "#8E8D8A"
        category_data.setdefault(category_name, {"amount": Decimal("0"), "color": category_color})
        category_data[category_name]["amount"] += Decimal(item.amount)
        if item.card:
            card_data.setdefault(item.card.name, {"amount": Decimal("0"), "color": item.card.color})
            card_data[item.card.name]["amount"] += Decimal(item.amount)
            origin_data["Cartões"] += Decimal(item.amount)
        else:
            origin_data["Contas e outros"] += Decimal(item.amount)
        daily_data[item.transaction_date.day] += Decimal(item.amount)

    categories = sorted(({"name": name, **values} for name, values in category_data.items()), key=lambda row: row["amount"], reverse=True)
    cards = sorted(({"name": name, **values} for name, values in card_data.items()), key=lambda row: row["amount"], reverse=True)
    trend = []
    for offset in range(-5, 1):
        month = shift_month(reference_month, offset)
        trend_start, trend_end = month_bounds(month)
        rows = Transaction.query.filter(Transaction.transaction_date.between(trend_start, trend_end)).all()
        trend.append({
            "label": month_label(month, short=True),
            "income": float(sum((Decimal(row.amount) for row in rows if row.kind == "income"), Decimal("0"))),
            "expenses": float(sum((Decimal(row.amount) for row in rows if row.kind == "expense"), Decimal("0"))),
        })

    elapsed_days = end.day
    if reference_month == date.today().strftime("%Y-%m"):
        elapsed_days = date.today().day
    average_daily = expenses / max(elapsed_days, 1)
    projected_month = average_daily * end.day if reference_month == date.today().strftime("%Y-%m") else expenses
    invested = investment_position()
    cash = cash_balance()
    patrimony = cash + invested
    future_rows = future_invoice_rows(reference_month, 12)
    future_total = sum((row["total"] for row in future_rows), Decimal("0"))

    guidance = []
    if expenses > income and expenses:
        guidance.append({"tone": "danger", "title": "Mês acima das entradas", "text": f"As saídas superam as entradas em {brl(expenses - income)}."})
    elif savings_rate is not None and savings_rate >= 20:
        guidance.append({"tone": "success", "title": "Boa capacidade de poupança", "text": f"Você preservou {savings_rate}% das entradas deste mês."})
    elif savings_rate is not None:
        guidance.append({"tone": "warning", "title": "Margem para poupar", "text": f"A taxa de poupança está em {savings_rate}%. Acompanhe os maiores grupos de gasto."})
    if categories and expenses and categories[0]["amount"] / expenses >= Decimal("0.40"):
        share = (categories[0]["amount"] / expenses * Decimal("100")).quantize(Decimal("1"))
        guidance.append({"tone": "warning", "title": "Gasto concentrado", "text": f"{categories[0]['name']} representa {share}% das despesas."})
    if future_rows and future_rows[0]["total"]:
        guidance.append({"tone": "info", "title": "Próxima fatura provisionada", "text": f"Já existem {brl(future_rows[0]['total'])} previstos para {future_rows[0]['label']}."})
    if not guidance:
        guidance.append({"tone": "info", "title": "Comece pelos registros", "text": "Quanto mais lançamentos categorizados, mais precisos serão os indicadores."})

    chart_data = {
        "trend": trend,
        "categories": {"labels": [row["name"] for row in categories], "values": [float(row["amount"]) for row in categories], "colors": [row["color"] for row in categories]},
        "cards": {"labels": [row["name"] for row in cards], "values": [float(row["amount"]) for row in cards], "colors": [row["color"] for row in cards]},
        "origins": {"labels": list(origin_data), "values": [float(value) for value in origin_data.values()]},
        "daily": {"labels": list(daily_data), "values": [float(value) for value in daily_data.values()]},
    }
    return render_template(
        "indicators.html", reference_month=reference_month, month_name=month_label(reference_month),
        income=income, expenses=expenses, net=net, savings_rate=savings_rate,
        income_change=percentage_change(income, previous_income), expense_change=percentage_change(expenses, previous_expenses),
        average_daily=average_daily, projected_month=projected_month, cash=cash, invested=invested,
        patrimony=patrimony, future_total=future_total, categories=categories, cards=cards,
        top_expenses=sorted(expense_items, key=lambda item: item.amount, reverse=True)[:8],
        guidance=guidance, chart_data=chart_data,
    )


@main.get("/proximas-faturas")
@login_required
def future_invoices():
    base_month = request.args.get("month") or date.today().strftime("%Y-%m")
    try:
        month_bounds(base_month)
    except (ValueError, IndexError):
        base_month = date.today().strftime("%Y-%m")
    rows = future_invoice_rows(base_month, 12)
    total = sum((row["total"] for row in rows), Decimal("0"))
    next_three = sum((row["total"] for row in rows[:3]), Decimal("0"))
    largest = max(rows, key=lambda row: row["total"]) if rows else None
    card_totals = {}
    for row in rows:
        for card in row["cards"]:
            card_totals.setdefault(card["name"], {"amount": Decimal("0"), "color": card["color"]})
            card_totals[card["name"]]["amount"] += card["amount"]
    chart_data = {
        "labels": [row["short_label"] for row in rows],
        "values": [float(row["total"]) for row in rows],
        "cards": [{"name": name, "amount": float(value["amount"]), "color": value["color"]} for name, value in card_totals.items()],
    }
    return render_template("future_invoices.html", base_month=base_month, rows=rows, total=total, next_three=next_three, largest=largest, chart_data=chart_data)


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
            update_card_pdf_settings(card)
            db.session.add(card); db.session.commit(); flash("Cartão adicionado.", "success"); return redirect(url_for("main.cards"))
        except (ValueError, InvalidOperation):
            db.session.rollback(); flash("Confira os dados do cartão.", "danger")
    return render_template("cards.html", cards=CreditCard.query.order_by(CreditCard.name).all(), invoice_providers=INVOICE_PROVIDERS, provider_labels=PROVIDER_LABELS)


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
                update_card_pdf_settings(card)
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
    return render_template("configure_card.html", card=card, cycles=cycles, month=month, default_closing=default_closing, default_due=default_due, invoice_providers=INVOICE_PROVIDERS)


@main.route("/faturas/importar", methods=["GET", "POST"])
@login_required
def import_invoice():
    cards = CreditCard.query.filter_by(active=True).all()
    if request.method == "POST":
        file = request.files.get("invoice")
        if not file or not file.filename or not file.filename.lower().endswith(".pdf"):
            flash("Selecione uma fatura em PDF.", "danger"); return render_template("import_invoice.html", cards=cards, drive_configured=drive_is_configured())
        try:
            reference_month = request.form["reference_month"]
            datetime.strptime(reference_month, "%Y-%m")
            parsed = parse_invoice_pdf(file.stream, reference_month, request.form.get("pdf_password", ""))
            if not parsed["items"]:
                flash("Não consegui localizar compras automaticamente nesse PDF.", "warning"); return render_template("import_invoice.html", cards=cards, drive_configured=drive_is_configured())
            card = db.get_or_404(CreditCard, int(request.form["card_id"]))
            invoice = create_invoice_draft(card, parsed, file.filename)
            db.session.commit()
            return redirect(url_for("main.review_invoice", invoice_id=invoice.id))
        except PdfPasswordRequired:
            db.session.rollback(); flash("Este PDF tem senha. Informe-a no campo Senha do PDF.", "warning")
        except PdfPasswordInvalid:
            db.session.rollback(); flash("A senha informada para o PDF está incorreta.", "danger")
        except Exception:
            db.session.rollback(); flash("Não foi possível ler o PDF. Tente outro arquivo.", "danger")
    return render_template("import_invoice.html", cards=cards, drive_configured=drive_is_configured())


@main.post("/faturas/sincronizar-drive")
@login_required
def sync_drive_invoices():
    try:
        reference_month = request.form["reference_month"]
        datetime.strptime(reference_month, "%Y-%m")
        cards = CreditCard.query.filter_by(active=True).all()
        drive_session, files = list_month_pdfs(reference_month)
    except (ValueError, DriveConfigurationError, DriveAccessError) as exc:
        flash(str(exc) or "Não foi possível consultar o Google Drive.", "danger")
        return redirect(url_for("main.import_invoice"))

    results = []
    for drive_file in files:
        filename = drive_file["name"]
        if Invoice.query.filter_by(drive_file_id=drive_file["id"]).first():
            results.append({"filename": filename, "status": "Já importada", "tone": "muted", "detail": "Nenhuma duplicação foi criada."})
            continue
        try:
            downloaded = download_pdf(drive_session, drive_file["id"]).getvalue()
            parsed = parse_with_saved_passwords(lambda data=downloaded: BytesIO(data), reference_month, cards)
            if not parsed["items"]:
                raise ValueError("Nenhuma compra foi encontrada.")
            adapter = parsed.get("adapter", "generic")
            matching_cards = [card for card in cards if card.invoice_provider == adapter]
            if not matching_cards:
                raise ValueError(f"Configure um cartão para o leitor {PROVIDER_LABELS.get(adapter, adapter)}.")
            if len(matching_cards) > 1:
                raise ValueError(f"Há mais de um cartão configurado como {PROVIDER_LABELS.get(adapter, adapter)}.")
            invoice = create_invoice_draft(
                matching_cards[0],
                parsed,
                filename,
                drive_file_id=drive_file["id"],
            )
            db.session.commit()
            results.append({
                "filename": filename,
                "status": "Pronta para revisar",
                "tone": "success",
                "detail": f"{len(parsed['items'])} compras • {PROVIDER_LABELS.get(adapter, adapter)}",
                "invoice_id": invoice.id,
            })
        except PdfPasswordRequired:
            db.session.rollback()
            results.append({"filename": filename, "status": "Senha necessária", "tone": "danger", "detail": "Cadastre a senha do PDF na configuração do cartão."})
        except (DriveAccessError, ValueError) as exc:
            db.session.rollback()
            results.append({"filename": filename, "status": "Não importada", "tone": "danger", "detail": str(exc)})
        except Exception:
            db.session.rollback()
            results.append({"filename": filename, "status": "Não importada", "tone": "danger", "detail": "O PDF não pôde ser processado."})

    if not files:
        results.append({"filename": "—", "status": "Pasta vazia", "tone": "muted", "detail": "Nenhum PDF foi encontrado para esse mês."})
    return render_template("sync_drive_results.html", results=results, reference_month=reference_month)


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
        official_total = suggestion.get("statement_total")
        invoice.total = Decimal(official_total) if official_total else total
        invoice.status = "confirmed"; db.session.commit()
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


@main.get("/investimentos/simulador")
@login_required
def investment_simulator():
    return render_template("investment_simulator.html")


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
