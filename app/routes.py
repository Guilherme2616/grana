import calendar
import csv
import json
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO

from flask import Blueprint, flash, redirect, render_template, request, send_file, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import extract, func

from .extensions import db
from .models import Account, AssetPrice, CardCycle, Category, CategoryRule, CreditCard, FinancialGoal, Investment, Invoice, InvoiceItem, InvoicePayment, MonthlyClose, RecurringTransaction, Transaction, TransactionSplit, User
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


def invoice_provider_hint(card):
    if card.invoice_provider:
        return card.invoice_provider
    identity = f"{card.name} {card.institution}".lower()
    if any(marker in identity for marker in ("banco do brasil", "ourocard", "smiles")):
        return "bb_smiles"
    if "itaú" in identity or "itau" in identity:
        return "itau"
    if "mercado pago" in identity:
        return "mercado_pago"
    if "inter" in identity:
        return "banco_inter"
    return ""


def invoice_filename_provider_hint(filename, cards):
    normalized = unicodedata.normalize("NFKD", filename or "").encode("ascii", "ignore").decode().lower()
    markers = (
        ("bb_smiles", ("smiles", "ourocard", "banco do brasil")),
        ("itau", ("itau",)),
        ("mercado_pago", ("mercado pago",)),
        ("banco_inter", ("banco inter", "inter")),
    )
    for provider, values in markers:
        if any(value in normalized for value in values):
            return provider
    for card in cards:
        card_name = unicodedata.normalize("NFKD", card.name or "").encode("ascii", "ignore").decode().lower().strip()
        if card_name and card_name in normalized:
            return invoice_provider_hint(card)
    return ""


def personal_value(item):
    value = getattr(item, "personal_amount", None)
    return money(item.amount if value is None else value)


def responsibility_values(amount, responsibility, personal_amount=None):
    if responsibility not in {"self", "parents", "shared"}:
        raise ValueError
    amount = money(amount)
    if responsibility == "self":
        return responsibility, amount
    if responsibility == "parents":
        return responsibility, Decimal("0.00")
    value = money(personal_amount)
    if value < 0 or value > amount:
        raise ValueError
    return responsibility, value


def form_decimal(name, default="0"):
    raw = request.form.get(name, default).strip().replace("R$", "").replace(".", "").replace(",", ".")
    return Decimal(raw)


def month_is_closed(value):
    reference = value.strftime("%Y-%m") if hasattr(value, "strftime") else str(value)[:7]
    return MonthlyClose.query.filter_by(reference_month=reference).first() is not None


def category_options(kind=None, active_only=True):
    query = Category.query
    if kind:
        query = query.filter_by(kind=kind)
    if active_only:
        query = query.filter_by(active=True)
    return sorted(query.all(), key=lambda item: (item.parent.name if item.parent else item.name, bool(item.parent), item.sort_order, item.name))


def automatic_category(description, kind="expense"):
    normalized = (description or "").casefold()
    rules = CategoryRule.query.join(Category).filter(CategoryRule.active.is_(True), Category.active.is_(True), Category.kind == kind).order_by(func.length(CategoryRule.pattern).desc()).all()
    for rule in rules:
        if rule.pattern.casefold() in normalized:
            return rule.category_id
    return None


def display_filename(filename):
    return (filename or "fatura.pdf").replace("\\", "/").rsplit("/", 1)[-1].strip()[:255]


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


def card_competence_month(card, purchase_date):
    cycle = (
        CardCycle.query.filter(CardCycle.card_id == card.id, CardCycle.closing_date >= purchase_date)
        .order_by(CardCycle.closing_date.asc()).first()
    )
    if cycle:
        return cycle.reference_month
    base_month = purchase_date.strftime("%Y-%m")
    for offset in range(0, 3):
        reference = shift_month(base_month, offset)
        closing_date, _ = default_cycle_dates(card, reference)
        if purchase_date <= closing_date:
            return reference
    return base_month


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
        original_filename=display_filename(filename),
        status="draft",
        source=parsed.get("adapter", "generic"),
        credit_limit=parsed.get("credit_limit"),
        cash_advance_total=parsed.get("cash_advance_total"),
        statement_total=parsed.get("statement_total"),
        drive_file_id=drive_file_id,
    )
    db.session.add(invoice)
    db.session.flush()
    for item in parsed["items"]:
        payload = dict(item)
        payload.setdefault("category_id", automatic_category(payload.get("description", "")))
        db.session.add(InvoiceItem(invoice_id=invoice.id, **payload))
    parsed_items_total = sum((item["amount"] for item in parsed["items"]), Decimal("0"))
    invoice.total = parsed.get("statement_total") or parsed_items_total
    closing_date, due_date = default_cycle_dates(card, reference_month, parsed["due_date"])
    invoice.suggested_closing_date = closing_date
    invoice.suggested_due_date = due_date
    invoice.date_source = "pdf" if parsed["due_date"] else "default"
    return invoice


def invoice_review_suggestion(invoice):
    closing_date = invoice.suggested_closing_date
    due_date = invoice.suggested_due_date
    if not closing_date or not due_date:
        closing_date, due_date = default_cycle_dates(invoice.card, invoice.reference_month)
    return {
        "closing_date": closing_date.isoformat(),
        "due_date": due_date.isoformat(),
        "source": invoice.date_source or "default",
        "statement_total": str(invoice.statement_total or ""),
        "adapter": invoice.source,
        "credit_limit": str(invoice.credit_limit or ""),
        "cash_advance_total": str(invoice.cash_advance_total or ""),
    }


def replace_invoice_draft(invoice, parsed):
    for item in list(invoice.items):
        db.session.delete(item)
    db.session.flush()
    for item in parsed["items"]:
        payload = dict(item)
        payload.setdefault("category_id", automatic_category(payload.get("description", "")))
        db.session.add(InvoiceItem(invoice_id=invoice.id, **payload))
    parsed_items_total = sum((item["amount"] for item in parsed["items"]), Decimal("0"))
    invoice.reference_month = parsed["reference_month"]
    invoice.source = parsed.get("adapter", "generic")
    invoice.credit_limit = parsed.get("credit_limit")
    invoice.cash_advance_total = parsed.get("cash_advance_total")
    invoice.statement_total = parsed.get("statement_total")
    invoice.total = invoice.statement_total or parsed_items_total
    closing_date, due_date = default_cycle_dates(
        invoice.card, invoice.reference_month, parsed.get("due_date")
    )
    invoice.suggested_closing_date = closing_date
    invoice.suggested_due_date = due_date
    invoice.date_source = "pdf" if parsed.get("due_date") else "default"


def parse_with_saved_passwords(stream_factory, reference_month, cards, expected_provider=""):
    passwords = [""]
    for card in cards:
        password = decrypt_secret(card.pdf_password_encrypted)
        if password and password not in passwords:
            passwords.append(password)

    password_error = None
    for password in passwords:
        try:
            return parse_invoice_pdf(stream_factory(), reference_month, password, expected_provider)
        except (PdfPasswordRequired, PdfPasswordInvalid) as exc:
            password_error = exc
    if password_error:
        raise PdfPasswordRequired("Nenhuma senha cadastrada desbloqueou este PDF.")
    raise ValueError("Não foi possível interpretar o PDF.")


def percentage_change(current, previous):
    if not previous:
        return None
    return ((current - previous) / previous * Decimal("100")).quantize(Decimal("0.1"))


def transaction_filters():
    return {key: request.args.get(key, "") for key in ("category_id", "subcategory_id", "card_id", "institution", "account_id", "kind", "necessity", "frequency", "installment", "min_value", "max_value", "q")}


def filter_transactions(rows, filters):
    selected_category = int(filters["subcategory_id"] or filters["category_id"] or 0)
    root = db.session.get(Category, selected_category) if selected_category else None
    allowed_category_ids = {root.id, *(child.id for child in root.children.all())} if root else None
    result = []
    for row in rows:
        row_category_ids = {split.category_id for split in row.splits} or ({row.category_id} if row.category_id else set())
        if allowed_category_ids and not row_category_ids.intersection(allowed_category_ids): continue
        if filters["card_id"] and row.card_id != int(filters["card_id"]): continue
        if filters["institution"] and (not row.card or row.card.institution != filters["institution"]): continue
        if filters["account_id"] and row.account_id != int(filters["account_id"]): continue
        if filters["kind"] and row.kind != filters["kind"]: continue
        categories = [split.category for split in row.splits] or ([row.category] if row.category else [])
        if filters["necessity"] and not any(c.necessity == filters["necessity"] for c in categories): continue
        if filters["frequency"] and not any(c.frequency == filters["frequency"] for c in categories): continue
        if filters["installment"] == "yes" and not row.installment_total: continue
        if filters["installment"] == "no" and row.installment_total: continue
        if filters["min_value"]:
            try:
                if Decimal(row.amount) < Decimal(filters["min_value"].replace(",", ".")): continue
            except InvalidOperation: pass
        if filters["max_value"]:
            try:
                if Decimal(row.amount) > Decimal(filters["max_value"].replace(",", ".")): continue
            except InvalidOperation: pass
        if filters["q"] and filters["q"].casefold() not in row.description.casefold(): continue
        result.append(row)
    return result


def investment_position():
    items = Investment.query.all()
    buys = sum((item.total_value for item in items if item.operation == "Compra"), Decimal("0"))
    sales = sum((item.total_value for item in items if item.operation == "Venda"), Decimal("0"))
    return buys - sales


def cash_balance():
    initial = money(db.session.query(func.sum(Account.initial_balance)).filter(Account.active.is_(True)).scalar())
    account_rows = Transaction.query.filter(
        Transaction.card_id.is_(None),
        Transaction.status == "confirmed",
        Transaction.transaction_date <= date.today(),
    ).all()
    income = sum((money(item.amount) for item in account_rows if item.kind == "income"), Decimal("0"))
    refunds = sum((money(item.amount) for item in account_rows if item.kind == "refund"), Decimal("0"))
    expenses = sum((money(item.amount) for item in account_rows if item.kind == "expense"), Decimal("0"))
    invoice_payments = sum((money(item.amount) for item in InvoicePayment.query.filter(
        InvoicePayment.paid_by == "self",
        InvoicePayment.payment_date <= date.today(),
    ).all()), Decimal("0"))
    return initial + income + refunds - expenses - invoice_payments


def outstanding_card_commitment():
    """Valor pessoal ainda comprometido nos cartões, sem duplicar a baixa."""
    total = Decimal("0")
    for invoice in Invoice.query.filter_by(status="confirmed").all():
        personal_total = sum((
            money(item.amount if item.personal_amount is None else item.personal_amount)
            for item in invoice.items if item.selected
        ), Decimal("0"))
        self_paid = sum((
            money(payment.amount) for payment in invoice.payments
            if payment.paid_by == "self" and payment.payment_date <= date.today()
        ), Decimal("0"))
        total += max(personal_total - self_paid, Decimal("0"))

    manual_card_rows = Transaction.query.filter(
        Transaction.card_id.isnot(None),
        Transaction.invoice_item_id.is_(None),
        Transaction.status == "confirmed",
        Transaction.transaction_date <= date.today(),
    ).all()
    total += sum((personal_value(item) for item in manual_card_rows if item.kind == "expense"), Decimal("0"))
    total -= sum((personal_value(item) for item in manual_card_rows if item.kind == "refund"), Decimal("0"))
    return max(total, Decimal("0"))


def transaction_reference_month(item):
    return item.competence_month or item.transaction_date.strftime("%Y-%m")


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
    cashflow = {}
    for item in Transaction.query.all():
        reference = transaction_reference_month(item)
        values = cashflow.setdefault(reference, {"income": Decimal("0"), "other_expenses": Decimal("0")})
        if item.kind == "income":
            values["income"] += money(item.amount)
        elif item.card_id is None and item.kind == "expense":
            values["other_expenses"] += personal_value(item)
        elif item.card_id is None and item.kind == "refund":
            values["other_expenses"] -= personal_value(item)

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
        card_total = sum(per_card.values(), Decimal("0"))
        month_cashflow = cashflow.get(reference, {"income": Decimal("0"), "other_expenses": Decimal("0")})
        income = month_cashflow["income"]
        other_expenses = month_cashflow["other_expenses"]
        expenses_total = card_total + other_expenses
        spending_groups = list(card_values)
        if other_expenses:
            spending_groups.append({
                "id": None,
                "name": "Demais gastos",
                "color": "#8E8D8A",
                "amount": other_expenses,
                "confirmed": True,
            })
        rows.append({
            "reference_month": reference,
            "label": month_label(reference),
            "short_label": month_label(reference, short=True),
            "total": expenses_total,
            "card_total": card_total,
            "income": income,
            "other_expenses": other_expenses,
            "expenses_total": expenses_total,
            "net": income - expenses_total,
            "cards": card_values,
            "spending_groups": spending_groups,
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
    all_transactions = Transaction.query.all()
    reference_month = request.args.get("month") or today.strftime("%Y-%m")
    try:
        month_bounds(reference_month)
    except (ValueError, IndexError):
        reference_month = today.strftime("%Y-%m")
    if not request.args.get("month") and all_transactions:
        current_rows = [item for item in all_transactions if transaction_reference_month(item) == reference_month]
        if not current_rows:
            reference_month = max(transaction_reference_month(item) for item in all_transactions)
    base = [item for item in all_transactions if transaction_reference_month(item) == reference_month]
    income = sum((money(item.amount) for item in base if item.kind == "income"), Decimal("0"))
    expenses = sum((personal_value(item) for item in base if item.kind == "expense"), Decimal("0")) - sum((personal_value(item) for item in base if item.kind == "refund"), Decimal("0"))
    cash = cash_balance()
    card_commitment = outstanding_card_commitment()
    balance = cash - card_commitment
    next_invoice = Invoice.query.filter_by(status="confirmed").order_by(Invoice.reference_month.desc()).first()
    transactions = Transaction.query.order_by(Transaction.transaction_date.desc(), Transaction.id.desc()).limit(5).all()
    return render_template(
        "dashboard.html", balance=balance, cash=cash, card_commitment=card_commitment,
        income=income, expenses=expenses, reference_month=reference_month,
        month_name=month_label(reference_month), next_invoice=next_invoice,
        transactions=transactions, today=today,
    )


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
    filters = transaction_filters()
    all_transactions = Transaction.query.all()
    month_items = filter_transactions([item for item in all_transactions if (item.competence_month or item.transaction_date.strftime("%Y-%m")) == reference_month], filters)
    previous_items = filter_transactions([item for item in all_transactions if (item.competence_month or item.transaction_date.strftime("%Y-%m")) == previous_month], filters)
    income = sum((Decimal(item.amount) for item in month_items if item.kind == "income"), Decimal("0"))
    expenses = sum((personal_value(item) for item in month_items if item.kind == "expense"), Decimal("0")) - sum((personal_value(item) for item in month_items if item.kind == "refund"), Decimal("0"))
    previous_income = sum((Decimal(item.amount) for item in previous_items if item.kind == "income"), Decimal("0"))
    previous_expenses = sum((personal_value(item) for item in previous_items if item.kind == "expense"), Decimal("0")) - sum((personal_value(item) for item in previous_items if item.kind == "refund"), Decimal("0"))
    net = income - expenses
    savings_rate = (net / income * Decimal("100")).quantize(Decimal("0.1")) if income else None

    category_data = {}
    card_data = {}
    origin_data = {"Cartões": Decimal("0"), "Contas e outros": Decimal("0")}
    daily_data = {day: Decimal("0") for day in range(1, end.day + 1)}
    expense_items = [item for item in month_items if item.kind == "expense"]
    for item in expense_items:
        ratio = personal_value(item) / money(item.amount) if money(item.amount) else Decimal("0")
        allocations = [(split.category, money(split.amount) * ratio) for split in item.splits] or [(item.category, personal_value(item))]
        for category, allocated_amount in allocations:
            category_name = category.full_name if category else "Sem categoria"
            category_color = category.color if category else "#8E8D8A"
            category_data.setdefault(category_name, {"amount": Decimal("0"), "color": category_color})
            category_data[category_name]["amount"] += allocated_amount
        if item.card:
            card_data.setdefault(item.card.name, {"amount": Decimal("0"), "color": item.card.color})
            card_data[item.card.name]["amount"] += personal_value(item)
            origin_data["Cartões"] += personal_value(item)
        else:
            origin_data["Contas e outros"] += personal_value(item)
        if item.transaction_date.month == start.month:
            daily_data[item.transaction_date.day] += personal_value(item)

    categories = sorted(({"name": name, **values} for name, values in category_data.items()), key=lambda row: row["amount"], reverse=True)
    cards = sorted(({"name": name, **values} for name, values in card_data.items()), key=lambda row: row["amount"], reverse=True)
    trend = []
    for offset in range(-5, 1):
        month = shift_month(reference_month, offset)
        trend_start, trend_end = month_bounds(month)
        rows = filter_transactions([item for item in all_transactions if (item.competence_month or item.transaction_date.strftime("%Y-%m")) == month], filters)
        trend.append({
            "label": month_label(month, short=True),
            "income": float(sum((Decimal(row.amount) for row in rows if row.kind == "income"), Decimal("0"))),
            "expenses": float(sum((personal_value(row) for row in rows if row.kind == "expense"), Decimal("0")) - sum((personal_value(row) for row in rows if row.kind == "refund"), Decimal("0"))),
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
    for category in Category.query.filter(Category.active.is_(True), Category.monthly_budget.isnot(None)).all():
        spent = sum((Decimal(split.amount) for row in expense_items for split in row.splits if split.category_id == category.id), Decimal("0"))
        spent += sum((Decimal(row.amount) for row in expense_items if not row.splits and row.category_id == category.id), Decimal("0"))
        if category.monthly_budget and spent >= Decimal(category.monthly_budget) * Decimal("0.90"):
            percent = (spent / Decimal(category.monthly_budget) * 100).quantize(Decimal("1"))
            guidance.append({"tone": "danger" if percent >= 100 else "warning", "title": f"Orçamento de {category.full_name}", "text": f"Você utilizou {percent}% do limite mensal definido."})
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
        guidance=guidance, chart_data=chart_data, filters=filters,
        filter_categories=Category.query.filter_by(parent_id=None, active=True).order_by(Category.name).all(),
        filter_subcategories=Category.query.filter(Category.parent_id.isnot(None), Category.active.is_(True)).order_by(Category.name).all(),
        filter_cards=CreditCard.query.filter_by(active=True).order_by(CreditCard.name).all(),
        filter_accounts=Account.query.filter_by(active=True).order_by(Account.name).all(),
        institutions=[row[0] for row in db.session.query(CreditCard.institution).filter(CreditCard.institution != "").distinct().order_by(CreditCard.institution).all()],
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
    total = sum((row["expenses_total"] for row in rows), Decimal("0"))
    total_income = sum((row["income"] for row in rows), Decimal("0"))
    total_net = total_income - total
    next_three = sum((row["expenses_total"] for row in rows[:3]), Decimal("0"))
    card_totals = {}
    for row in rows:
        for card in row["cards"]:
            card_totals.setdefault(card["name"], {"amount": Decimal("0"), "color": card["color"]})
            card_totals[card["name"]]["amount"] += card["amount"]
        if row["other_expenses"]:
            card_totals.setdefault("Demais gastos", {"amount": Decimal("0"), "color": "#8E8D8A"})
            card_totals["Demais gastos"]["amount"] += row["other_expenses"]
    chart_data = {
        "labels": [row["short_label"] for row in rows],
        "expenses": [float(row["expenses_total"]) for row in rows],
        "income": [float(row["income"]) for row in rows],
        "cards": [{"name": name, "amount": float(value["amount"]), "color": value["color"]} for name, value in card_totals.items()],
    }
    return render_template(
        "future_invoices.html", base_month=base_month, rows=rows, total=total,
        total_income=total_income, total_net=total_net, next_three=next_three,
        chart_data=chart_data,
    )


@main.route("/movimentacoes", methods=["GET", "POST"])
@login_required
def transactions():
    if request.method == "POST":
        try:
            transaction_date = datetime.strptime(request.form["transaction_date"], "%Y-%m-%d").date()
            if month_is_closed(transaction_date):
                flash("Este mês está fechado. Reabra-o antes de adicionar lançamentos.", "warning"); return redirect(url_for("main.transactions"))
            category_id = request.form.get("category_id") or automatic_category(request.form["description"], request.form["kind"])
            amount = form_decimal("amount")
            card = db.session.get(CreditCard, int(request.form["card_id"])) if request.form.get("card_id") else None
            responsibility, personal_amount = responsibility_values(
                amount,
                request.form.get("payment_responsibility", "self"),
                request.form.get("personal_amount", "0").replace(".", "").replace(",", "."),
            )
            transaction = Transaction(
                description=request.form["description"].strip(), amount=amount,
                kind=request.form["kind"], transaction_date=transaction_date,
                account_id=request.form.get("account_id") or None, category_id=category_id,
                card_id=card.id if card else None, notes=request.form.get("notes", "").strip(),
                competence_month=card_competence_month(card, transaction_date) if card else transaction_date.strftime("%Y-%m"),
                payment_responsibility=responsibility, personal_amount=personal_amount,
            )
            if not transaction.description or transaction.amount <= 0:
                raise ValueError
            db.session.add(transaction); db.session.commit(); flash("Lançamento salvo.", "success")
            return redirect(url_for("main.transactions"))
        except (ValueError, InvalidOperation):
            db.session.rollback(); flash("Confira a descrição, o valor e a data.", "danger")
    query = Transaction.query
    start, end, q = request.args.get("start", ""), request.args.get("end", ""), request.args.get("q", "")
    if start: query = query.filter(Transaction.transaction_date >= datetime.strptime(start, "%Y-%m-%d").date())
    if end: query = query.filter(Transaction.transaction_date <= datetime.strptime(end, "%Y-%m-%d").date())
    if q: query = query.filter(Transaction.description.ilike(f"%{q}%"))
    raw_items = query.order_by(Transaction.transaction_date.desc(), Transaction.id.desc()).all()
    filters = transaction_filters()
    items = filter_transactions(raw_items, filters)
    return render_template("transactions.html", transactions=items, accounts=Account.query.filter_by(active=True).all(), categories=category_options(), cards=CreditCard.query.filter_by(active=True).all(), institutions=[row[0] for row in db.session.query(CreditCard.institution).filter(CreditCard.institution != "").distinct().all()], today=date.today(), filters={**filters, "start":start, "end":end})


@main.route("/movimentacoes/<int:item_id>/editar", methods=["GET", "POST"])
@login_required
def edit_transaction(item_id):
    item = db.get_or_404(Transaction, item_id)
    if month_is_closed(item.transaction_date):
        flash("Este mês está fechado. Reabra-o antes de editar.", "warning"); return redirect(url_for("main.transactions"))
    if request.method == "POST":
        try:
            item.description = request.form["description"].strip()
            item.amount = form_decimal("amount")
            item.kind = request.form["kind"]
            item.transaction_date = datetime.strptime(request.form["transaction_date"], "%Y-%m-%d").date()
            item.category_id = request.form.get("category_id") or None
            item.account_id = request.form.get("account_id") or None
            card = db.session.get(CreditCard, int(request.form["card_id"])) if request.form.get("card_id") else None
            item.card_id = card.id if card else None
            item.competence_month = card_competence_month(card, item.transaction_date) if card else item.transaction_date.strftime("%Y-%m")
            item.payment_responsibility, item.personal_amount = responsibility_values(
                item.amount,
                request.form.get("payment_responsibility", "self"),
                request.form.get("personal_amount", "0").replace(".", "").replace(",", "."),
            )
            item.notes = request.form.get("notes", "").strip()
            item.installment_current = int(request.form["installment_current"]) if request.form.get("installment_current") else None
            item.installment_total = int(request.form["installment_total"]) if request.form.get("installment_total") else None
            if not item.description or item.amount <= 0: raise ValueError
            db.session.commit(); flash("Lançamento atualizado.", "success"); return redirect(url_for("main.transactions"))
        except (ValueError, InvalidOperation):
            db.session.rollback(); flash("Confira os dados do lançamento.", "danger")
    return render_template("edit_transaction.html", item=item, categories=category_options(), accounts=Account.query.filter_by(active=True).all(), cards=CreditCard.query.filter_by(active=True).all())


@main.post("/movimentacoes/<int:item_id>/excluir")
@login_required
def delete_transaction(item_id):
    item = db.get_or_404(Transaction, item_id)
    if month_is_closed(item.transaction_date): flash("Este mês está fechado. Reabra-o antes de excluir.", "warning"); return redirect(url_for("main.transactions"))
    db.session.delete(item); db.session.commit(); flash("Lançamento excluído.", "success")
    return redirect(url_for("main.transactions"))


@main.route("/movimentacoes/<int:item_id>/dividir", methods=["GET", "POST"])
@login_required
def split_transaction(item_id):
    item = db.get_or_404(Transaction, item_id)
    categories = category_options(item.kind)
    if request.method == "POST":
        try:
            category_ids = request.form.getlist("split_category_id")
            amounts = request.form.getlist("split_amount")
            parts = []
            for category_id, raw_amount in zip(category_ids, amounts):
                amount = Decimal(raw_amount.strip().replace(".", "").replace(",", "."))
                category = db.session.get(Category, int(category_id))
                if not category or category.kind != item.kind or amount <= 0:
                    raise ValueError
                parts.append((category.id, amount))
            if len(parts) < 2 or sum((part[1] for part in parts), Decimal("0")) != Decimal(item.amount):
                raise ValueError
            TransactionSplit.query.filter_by(transaction_id=item.id).delete()
            for category_id, amount in parts:
                db.session.add(TransactionSplit(transaction_id=item.id, category_id=category_id, amount=amount))
            item.category_id = None
            db.session.commit(); flash("Compra dividida entre as categorias.", "success"); return redirect(url_for("main.transactions"))
        except (ValueError, InvalidOperation):
            db.session.rollback(); flash("A divisão precisa ter ao menos duas partes e somar exatamente o valor da compra.", "danger")
    return render_template("split_transaction.html", item=item, categories=categories)


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
        try:
            name = request.form["name"].strip()
            kind = request.form.get("kind", "expense")
            parent_id = request.form.get("parent_id") or None
            parent = db.session.get(Category, int(parent_id)) if parent_id else None
            if not name or kind not in {"expense", "income", "transfer", "investment", "refund"} or (parent and (parent.parent_id or parent.kind != kind)):
                raise ValueError
            duplicate = Category.query.filter(func.lower(Category.name) == name.lower(), Category.parent_id == parent_id).first()
            if duplicate:
                flash("Já existe uma categoria com esse nome neste nível.", "warning")
                return redirect(url_for("main.categories"))
            budget = form_decimal("monthly_budget") if request.form.get("monthly_budget", "").strip() else None
            db.session.add(Category(name=name, kind=kind, parent_id=parent_id, color=request.form.get("color", "#D8B56A"), icon=request.form.get("icon", "$"), necessity=request.form.get("necessity", "essential"), frequency=request.form.get("frequency", "variable"), monthly_budget=budget))
            db.session.commit(); flash("Categoria adicionada.", "success"); return redirect(url_for("main.categories"))
        except (ValueError, InvalidOperation):
            db.session.rollback(); flash("Confira os dados da categoria.", "danger")
    roots = Category.query.filter_by(parent_id=None).order_by(Category.kind, Category.sort_order, Category.name).all()
    return render_template("categories.html", roots=roots, categories=category_options(active_only=False), rules=CategoryRule.query.order_by(CategoryRule.pattern).all())


@main.route("/categorias/<int:category_id>/editar", methods=["GET", "POST"])
@login_required
def edit_category(category_id):
    item = db.get_or_404(Category, category_id)
    if request.method == "POST":
        try:
            name = request.form["name"].strip()
            if not name:
                raise ValueError
            item.name, item.color, item.icon = name, request.form.get("color", item.color), request.form.get("icon", item.icon)
            item.necessity, item.frequency = request.form.get("necessity", "essential"), request.form.get("frequency", "variable")
            item.monthly_budget = form_decimal("monthly_budget") if request.form.get("monthly_budget", "").strip() else None
            item.active = request.form.get("active") == "on"
            db.session.commit(); flash("Categoria atualizada.", "success"); return redirect(url_for("main.categories"))
        except (ValueError, InvalidOperation):
            db.session.rollback(); flash("Confira os dados da categoria.", "danger")
    alternatives = [row for row in category_options(item.kind, active_only=False) if row.id != item.id]
    return render_template("edit_category.html", item=item, alternatives=alternatives)


@main.post("/categorias/<int:category_id>/excluir")
@login_required
def delete_category(category_id):
    item = db.get_or_404(Category, category_id)
    if item.protected:
        flash("Esta é uma categoria protegida e não pode ser excluída.", "warning"); return redirect(url_for("main.categories"))
    replacement_id = request.form.get("replacement_id") or None
    replacement = db.session.get(Category, int(replacement_id)) if replacement_id else None
    if replacement and (replacement.id == item.id or replacement.kind != item.kind):
        flash("A categoria de destino é inválida.", "danger"); return redirect(url_for("main.categories"))
    affected = Transaction.query.filter_by(category_id=item.id).count() + InvoiceItem.query.filter_by(category_id=item.id).count() + TransactionSplit.query.filter_by(category_id=item.id).count()
    if affected and request.form.get("confirm_reassign") != "yes":
        flash("Escolha um destino e confirme a reatribuição antes de excluir.", "warning"); return redirect(url_for("main.categories"))
    for child in item.children.all():
        child.parent_id = None
    Transaction.query.filter_by(category_id=item.id).update({"category_id": replacement.id if replacement else None})
    InvoiceItem.query.filter_by(category_id=item.id).update({"category_id": replacement.id if replacement else None})
    if replacement:
        TransactionSplit.query.filter_by(category_id=item.id).update({"category_id": replacement.id})
    else:
        TransactionSplit.query.filter_by(category_id=item.id).delete()
    db.session.delete(item); db.session.commit(); flash("Categoria excluída e histórico preservado.", "success")
    return redirect(url_for("main.categories"))


@main.post("/categorias/regras")
@login_required
def add_category_rule():
    pattern = request.form.get("pattern", "").strip()
    category = db.session.get(Category, int(request.form.get("category_id", 0)))
    if not pattern or not category:
        flash("Informe o texto e a categoria da regra.", "danger")
    else:
        db.session.add(CategoryRule(pattern=pattern, category_id=category.id)); db.session.commit(); flash("Regra automática adicionada.", "success")
    return redirect(url_for("main.categories"))


@main.post("/categorias/regras/<int:rule_id>/excluir")
@login_required
def delete_category_rule(rule_id):
    db.session.delete(db.get_or_404(CategoryRule, rule_id)); db.session.commit(); flash("Regra removida.", "success")
    return redirect(url_for("main.categories"))


@main.route("/cartoes", methods=["GET", "POST"])
@login_required
def cards():
    if request.method == "POST":
        try:
            card = CreditCard(name=request.form["name"].strip(), institution=request.form.get("institution", "").strip(), last_digits=request.form.get("last_digits", "")[-4:], credit_limit=form_decimal("credit_limit"), closing_day=int(request.form["closing_day"]), due_day=int(request.form["due_day"]), color=request.form.get("color", "#173F35"))
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
                card.institution = request.form.get("institution", "").strip()
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
            card = db.get_or_404(CreditCard, int(request.form["card_id"]))
            parsed = parse_invoice_pdf(
                file.stream,
                reference_month,
                request.form.get("pdf_password", ""),
                invoice_provider_hint(card),
            )
            if not parsed["items"]:
                flash("Não consegui localizar compras automaticamente nesse PDF.", "warning"); return render_template("import_invoice.html", cards=cards, drive_configured=drive_is_configured())
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


@main.get("/faturas")
@login_required
def invoices():
    status = request.args.get("status", "all")
    query = Invoice.query
    if status in {"draft", "confirmed"}:
        query = query.filter_by(status=status)
    invoice_rows = []
    for invoice in query.order_by(Invoice.created_at.desc()).all():
        recognized = sum((Decimal(item.amount) for item in invoice.items if item.selected), Decimal("0"))
        personal_total = sum((money(item.amount if item.personal_amount is None else item.personal_amount) for item in invoice.items if item.selected), Decimal("0"))
        paid = sum((money(payment.amount) for payment in invoice.payments), Decimal("0"))
        invoice_rows.append({"invoice":invoice,"recognized":recognized,"difference":Decimal(invoice.total or 0)-recognized,"personal_total":personal_total,"paid":paid,"outstanding":max(Decimal(invoice.total or 0)-paid, Decimal("0"))})
    pending_count = Invoice.query.filter_by(status="draft").count()
    return render_template(
        "invoices.html",
        invoices=invoice_rows,
        current_status=status,
        pending_count=pending_count,
        accounts=Account.query.filter_by(active=True).order_by(Account.name).all(),
    )


@main.post("/faturas/<int:invoice_id>/pagamentos")
@login_required
def add_invoice_payment(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    if invoice.status != "confirmed":
        flash("Confirme a fatura antes de registrar pagamentos.", "warning")
        return redirect(url_for("main.invoices"))
    try:
        amount = form_decimal("amount")
        payment_date = datetime.strptime(request.form["payment_date"], "%Y-%m-%d").date()
        paid_by = request.form.get("paid_by", "self")
        account_id = request.form.get("account_id") or None
        paid = sum((money(payment.amount) for payment in invoice.payments), Decimal("0"))
        if paid_by not in {"self", "parents"} or amount <= 0 or paid + amount > money(invoice.total):
            raise ValueError
        if paid_by == "self" and not account_id:
            raise ValueError
        db.session.add(InvoicePayment(invoice_id=invoice.id, account_id=account_id if paid_by == "self" else None, amount=amount, payment_date=payment_date, paid_by=paid_by, notes=request.form.get("notes", "").strip()))
        db.session.commit()
        flash("Pagamento registrado como baixa da fatura, sem duplicar a despesa.", "success")
    except (ValueError, InvalidOperation):
        db.session.rollback()
        flash("Confira o valor, a data e a conta do pagamento.", "danger")
    return redirect(url_for("main.invoices", status="confirmed"))


@main.post("/faturas/<int:invoice_id>/total")
@login_required
def update_invoice_total(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    if MonthlyClose.query.filter_by(reference_month=invoice.reference_month).first():
        flash("A competência desta fatura está fechada. Reabra o mês antes de editar o total.", "warning")
        return redirect(url_for("main.invoices"))
    try:
        total = form_decimal("total")
        paid = sum((money(payment.amount) for payment in invoice.payments), Decimal("0"))
        if total < 0 or total < paid:
            raise ValueError
        invoice.total = total
        invoice.statement_total = total
        db.session.commit()
        flash("Total da fatura atualizado. As compras importadas foram preservadas.", "success")
    except (ValueError, InvalidOperation):
        db.session.rollback()
        flash("Informe um total válido, igual ou maior que o valor já pago.", "danger")
    return redirect(url_for("main.invoices", status=request.form.get("status", "confirmed")))


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
        existing = Invoice.query.filter_by(drive_file_id=drive_file["id"]).first()
        if existing:
            if existing.status == "draft":
                results.append({
                    "filename": filename,
                    "status": "Aguardando revisão",
                    "tone": "warning",
                    "detail": "A importação já foi iniciada. Continue de onde parou.",
                    "invoice_id": existing.id,
                })
            else:
                results.append({"filename": filename, "status": "Já importada", "tone": "muted", "detail": "Nenhuma duplicação foi criada."})
            continue
        try:
            downloaded = download_pdf(drive_session, drive_file["id"]).getvalue()
            parsed = parse_with_saved_passwords(
                lambda data=downloaded: BytesIO(data),
                reference_month,
                cards,
                invoice_filename_provider_hint(filename, cards),
            )
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
    categories = category_options("expense")
    suggestion = invoice_review_suggestion(invoice)
    if request.method == "POST":
        selected_ids = {int(value) for value in request.form.getlist("selected")}
        closing_date = datetime.strptime(request.form["closing_date"], "%Y-%m-%d").date()
        due_date = datetime.strptime(request.form["due_date"], "%Y-%m-%d").date()
        if closing_date >= due_date:
            flash("O fechamento precisa acontecer antes do vencimento.", "danger")
            return render_template("review_invoice.html", invoice=invoice, categories=categories, suggestion=suggestion)
        try:
            official_total = form_decimal("official_total")
            if official_total < 0:
                raise ValueError
        except (ValueError, InvalidOperation):
            flash("Informe um valor total válido para a fatura.", "danger")
            return render_template("review_invoice.html", invoice=invoice, categories=categories, suggestion=suggestion)
        total = Decimal("0")
        for item in invoice.items:
            item.selected = item.id in selected_ids
            item.description = request.form.get(f"description_{item.id}", item.description).strip()[:180]
            try: item.amount = Decimal(request.form.get(f"amount_{item.id}", str(item.amount)).replace(".", "").replace(",", "."))
            except InvalidOperation: pass
            item.category_id = request.form.get(f"category_{item.id}") or automatic_category(item.description)
            responsibility, personal_amount = responsibility_values(
                item.amount,
                request.form.get(f"responsibility_{item.id}", "self"),
                request.form.get(f"personal_amount_{item.id}", "0").replace(".", "").replace(",", "."),
            )
            item.payment_responsibility = responsibility
            item.personal_amount = personal_amount
            if item.selected:
                total += item.amount
                db.session.add(Transaction(description=item.description, amount=item.amount, kind="expense", transaction_date=item.purchase_date, card_id=invoice.card_id, category_id=item.category_id, invoice_item_id=item.id, source="invoice", installment_current=item.installment_current, installment_total=item.installment_total, competence_month=invoice.reference_month, payment_responsibility=responsibility, personal_amount=personal_amount))
        source = "pdf" if request.form.get("date_source") == "pdf" else "manual"
        upsert_card_cycle(invoice.card, invoice.reference_month, closing_date, due_date, source)
        invoice.statement_total = official_total
        invoice.total = official_total
        invoice.status = "confirmed"; db.session.commit()
        session.pop(f"invoice_cycle_{invoice.id}", None)
        flash(f"Fatura importada com {len(selected_ids)} compras e datas atualizadas.", "success")
        return redirect(url_for("main.dashboard"))
    return render_template("review_invoice.html", invoice=invoice, categories=categories, suggestion=suggestion)


@main.post("/faturas/<int:invoice_id>/descartar")
@login_required
def discard_invoice(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    if invoice.status != "draft":
        flash("Somente faturas aguardando revisão podem ser descartadas.", "warning")
        return redirect(url_for("main.invoices"))
    filename = invoice.original_filename or "Fatura"
    session.pop(f"invoice_cycle_{invoice.id}", None)
    db.session.delete(invoice)
    db.session.commit()
    flash(f"{filename} foi descartada e já pode ser importada novamente.", "success")
    return redirect(url_for("main.invoices", status="draft"))


@main.post("/faturas/<int:invoice_id>/excluir")
@login_required
def delete_invoice_data(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    if MonthlyClose.query.filter_by(reference_month=invoice.reference_month).first():
        flash("A competência desta fatura está fechada. Reabra o mês antes de excluir os dados.", "warning")
        return redirect(url_for("main.invoices"))

    filename = invoice.original_filename or f"Fatura {invoice.reference_month}"
    item_ids = [item.id for item in invoice.items]
    imported_transactions = Transaction.query.filter(
        Transaction.invoice_item_id.in_(item_ids)
    ).all() if item_ids else []
    payment_count = len(invoice.payments)

    for transaction in imported_transactions:
        db.session.delete(transaction)
    db.session.flush()
    session.pop(f"invoice_cycle_{invoice.id}", None)
    db.session.delete(invoice)
    db.session.commit()

    flash(
        f"{filename} excluída: {len(imported_transactions)} compras e {payment_count} pagamentos removidos. O PDF já pode ser importado novamente.",
        "success",
    )
    return redirect(url_for("main.invoices"))


@main.route("/recorrencias", methods=["GET", "POST"])
@login_required
def recurring_transactions():
    if request.method == "POST":
        try:
            item = RecurringTransaction(description=request.form["description"].strip(), amount=form_decimal("amount"), kind=request.form.get("kind", "expense"), frequency=request.form.get("frequency", "monthly"), day=int(request.form.get("day", 1)), start_date=datetime.strptime(request.form["start_date"], "%Y-%m-%d").date(), end_date=datetime.strptime(request.form["end_date"], "%Y-%m-%d").date() if request.form.get("end_date") else None, category_id=request.form.get("category_id") or None, account_id=request.form.get("account_id") or None, card_id=request.form.get("card_id") or None, auto_create=bool(request.form.get("auto_create")), notes=request.form.get("notes", "").strip())
            if not item.description or item.amount <= 0 or not 1 <= item.day <= 31: raise ValueError
            db.session.add(item); db.session.commit(); flash("Recorrência cadastrada.", "success"); return redirect(url_for("main.recurring_transactions"))
        except (ValueError, InvalidOperation):
            db.session.rollback(); flash("Confira os dados da recorrência.", "danger")
    return render_template("recurring.html", items=RecurringTransaction.query.order_by(RecurringTransaction.active.desc(), RecurringTransaction.description).all(), categories=category_options(), accounts=Account.query.filter_by(active=True).all(), cards=CreditCard.query.filter_by(active=True).all(), today=date.today())


@main.post("/recorrencias/<int:item_id>/alternar")
@login_required
def toggle_recurring(item_id):
    item = db.get_or_404(RecurringTransaction, item_id); item.active = not item.active; db.session.commit(); flash("Recorrência atualizada.", "success")
    return redirect(url_for("main.recurring_transactions"))


def materialize_recurring(reference_month):
    start, end = month_bounds(reference_month)
    for recurring in RecurringTransaction.query.filter_by(active=True, auto_create=True).all():
        event_date = safe_date(start.year, start.month, recurring.day)
        if event_date < recurring.start_date or (recurring.end_date and event_date > recurring.end_date): continue
        if not Transaction.query.filter_by(recurring_id=recurring.id, transaction_date=event_date).first():
            db.session.add(Transaction(description=recurring.description, amount=recurring.amount, kind=recurring.kind, transaction_date=event_date, category_id=recurring.category_id, account_id=recurring.account_id, card_id=recurring.card_id, recurring_id=recurring.id, source="recurring", status="planned" if event_date > date.today() else "confirmed", notes=recurring.notes))
    db.session.commit()


@main.get("/calendario")
@login_required
def financial_calendar():
    reference_month = request.args.get("month") or date.today().strftime("%Y-%m")
    start, end = month_bounds(reference_month)
    materialize_recurring(reference_month)
    events = []
    for row in Transaction.query.filter(Transaction.transaction_date.between(start, end)).all():
        events.append({"date":row.transaction_date,"title":row.description,"amount":row.amount,"tone":"income" if row.kind == "income" else "expense","detail":"Previsto" if row.status == "planned" else "Realizado"})
    for cycle in CardCycle.query.filter(CardCycle.due_date.between(start, end)).all():
        events.append({"date":cycle.due_date,"title":f"Fatura {cycle.card.name}","amount":None,"tone":"card","detail":"Vencimento"})
    return render_template("calendar.html", reference_month=reference_month, month_name=month_label(reference_month), events=sorted(events, key=lambda row: row["date"]))


@main.route("/metas", methods=["GET", "POST"])
@login_required
def goals():
    if request.method == "POST":
        try:
            goal = FinancialGoal(name=request.form["name"].strip(), target_amount=form_decimal("target_amount"), current_amount=form_decimal("current_amount"), target_date=datetime.strptime(request.form["target_date"], "%Y-%m-%d").date() if request.form.get("target_date") else None, color=request.form.get("color", "#D8B56A"), notes=request.form.get("notes", "").strip())
            if not goal.name or goal.target_amount <= 0: raise ValueError
            db.session.add(goal); db.session.commit(); flash("Meta criada.", "success"); return redirect(url_for("main.goals"))
        except (ValueError, InvalidOperation):
            db.session.rollback(); flash("Confira os dados da meta.", "danger")
    return render_template("goals.html", goals=FinancialGoal.query.order_by(FinancialGoal.active.desc(), FinancialGoal.target_date).all())


@main.post("/metas/<int:goal_id>/aportar")
@login_required
def contribute_goal(goal_id):
    goal = db.get_or_404(FinancialGoal, goal_id)
    try:
        goal.current_amount = Decimal(goal.current_amount) + form_decimal("amount"); db.session.commit(); flash("Aporte registrado na meta.", "success")
    except InvalidOperation:
        db.session.rollback(); flash("Informe um aporte válido.", "danger")
    return redirect(url_for("main.goals"))


@main.get("/alertas")
@login_required
def alerts():
    today = date.today(); alerts_list = []
    for row in Transaction.query.filter(Transaction.status == "planned", Transaction.transaction_date <= today).all():
        alerts_list.append({"tone":"danger","title":"Conta prevista vencida","text":f"{row.description} • {brl(row.amount)} • {row.transaction_date.strftime('%d/%m')}"})
    for card in CreditCard.query.filter_by(active=True).all():
        used = money(db.session.query(func.sum(Transaction.amount)).filter_by(card_id=card.id, kind="expense").scalar())
        if card.credit_limit and used / Decimal(card.credit_limit) >= Decimal("0.80"):
            alerts_list.append({"tone":"warning","title":f"Limite do {card.name}","text":f"Utilização acumulada de {(used/Decimal(card.credit_limit)*100).quantize(Decimal('1'))}%."})
    for category in Category.query.filter(Category.monthly_budget.isnot(None), Category.active.is_(True)).all():
        start, end = month_bounds(today.strftime("%Y-%m")); spent = money(db.session.query(func.sum(Transaction.amount)).filter(Transaction.category_id == category.id, Transaction.kind == "expense", Transaction.transaction_date.between(start,end)).scalar())
        if category.monthly_budget and spent >= Decimal(category.monthly_budget)*Decimal("0.9"):
            alerts_list.append({"tone":"warning","title":f"Orçamento: {category.full_name}","text":f"{brl(spent)} de {brl(category.monthly_budget)} utilizados."})
    duplicates=db.session.query(Transaction.description,Transaction.amount,Transaction.transaction_date,func.count(Transaction.id)).group_by(Transaction.description,Transaction.amount,Transaction.transaction_date).having(func.count(Transaction.id)>1).all()
    for description,amount,transaction_date,count in duplicates[:5]:
        alerts_list.append({"tone":"warning","title":"Possível lançamento duplicado","text":f"{description} aparece {count} vezes em {transaction_date.strftime('%d/%m')} com valor {brl(amount)}."})
    return render_template("alerts.html", alerts=alerts_list)


@main.get("/planejamento")
@login_required
def budget_planning():
    reference_month=request.args.get("month") or date.today().strftime("%Y-%m"); start,end=month_bounds(reference_month)
    elapsed=date.today().day if reference_month==date.today().strftime("%Y-%m") else end.day
    rows=[]
    for category in Category.query.filter(Category.active.is_(True),Category.monthly_budget.isnot(None)).order_by(Category.name).all():
        direct=money(db.session.query(func.sum(Transaction.amount)).filter(Transaction.category_id==category.id,Transaction.kind=="expense",Transaction.transaction_date.between(start,end)).scalar())
        split=money(db.session.query(func.sum(TransactionSplit.amount)).join(Transaction).filter(TransactionSplit.category_id==category.id,Transaction.transaction_date.between(start,end)).scalar())
        spent=direct+split; budget=Decimal(category.monthly_budget); projected=(spent/max(elapsed,1))*end.day; percent=(spent/budget*100).quantize(Decimal("1")) if budget else Decimal("0")
        rows.append({"category":category,"budget":budget,"spent":spent,"remaining":budget-spent,"projected":projected,"percent":percent})
    return render_template("budget_planning.html",reference_month=reference_month,rows=rows,total_budget=sum((x["budget"] for x in rows),Decimal("0")),total_spent=sum((x["spent"] for x in rows),Decimal("0")))


@main.route("/fechamento", methods=["GET", "POST"])
@login_required
def monthly_closing():
    reference_month = request.values.get("month") or date.today().strftime("%Y-%m")
    start, end = month_bounds(reference_month); rows = Transaction.query.filter(Transaction.transaction_date.between(start,end), Transaction.status == "confirmed").all()
    income = sum((Decimal(row.amount) for row in rows if row.kind == "income"), Decimal("0")); expenses = sum((Decimal(row.amount) for row in rows if row.kind == "expense"), Decimal("0"))
    closed = MonthlyClose.query.filter_by(reference_month=reference_month).first()
    if request.method == "POST" and request.form.get("action") == "close" and not closed:
        snapshot = {"transactions":len(rows),"top":[row.description for row in sorted(rows,key=lambda x:x.amount,reverse=True)[:5]]}
        db.session.add(MonthlyClose(reference_month=reference_month,income=income,expenses=expenses,balance=income-expenses,snapshot_json=json.dumps(snapshot,ensure_ascii=False))); db.session.commit(); flash("Mês fechado e protegido.", "success"); return redirect(url_for("main.monthly_closing",month=reference_month))
    if request.method == "POST" and request.form.get("action") == "reopen" and closed:
        db.session.delete(closed); db.session.commit(); flash("Mês reaberto.", "success"); return redirect(url_for("main.monthly_closing",month=reference_month))
    return render_template("monthly_closing.html", reference_month=reference_month, closed=closed, income=income, expenses=expenses, net=income-expenses, rows=rows)


@main.get("/dados/exportar.csv")
@login_required
def export_transactions():
    month = request.args.get("month"); query = Transaction.query
    if month:
        start,end=month_bounds(month); query=query.filter(Transaction.transaction_date.between(start,end))
    rows=filter_transactions(query.order_by(Transaction.transaction_date).all(), transaction_filters())
    stream=StringIO(); writer=csv.writer(stream,delimiter=";"); writer.writerow(["Data","Descrição","Tipo","Valor","Categoria","Conta","Cartão","Banco","Origem"])
    for row in rows: writer.writerow([row.transaction_date.strftime("%d/%m/%Y"),row.description,row.kind,str(row.amount).replace(".",","),row.category.full_name if row.category else "",row.account.name if row.account else "",row.card.name if row.card else "",row.card.institution if row.card else "",row.source])
    data=BytesIO(stream.getvalue().encode("utf-8-sig")); return send_file(data,mimetype="text/csv",as_attachment=True,download_name=f"grana-{month or 'movimentacoes'}.csv")


def build_simple_pdf(lines):
    escaped=[str(line).encode("latin-1","replace").decode("latin-1").replace("\\","\\\\").replace("(","\\(").replace(")","\\)") for line in lines]
    content="BT /F1 11 Tf 45 800 Td 14 TL "+" ".join(f"({line}) Tj T*" for line in escaped)+" ET"
    objects=["<< /Type /Catalog /Pages 2 0 R >>","<< /Type /Pages /Kids [3 0 R] /Count 1 >>","<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",f"<< /Length {len(content.encode('latin-1'))} >>\nstream\n{content}\nendstream","<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    pdf=bytearray(b"%PDF-1.4\n"); offsets=[]
    for index,obj in enumerate(objects,1): offsets.append(len(pdf)); pdf.extend(f"{index} 0 obj\n{obj}\nendobj\n".encode("latin-1"))
    xref=len(pdf); pdf.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets: pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()); return bytes(pdf)


@main.get("/dados/relatorio.pdf")
@login_required
def monthly_pdf_report():
    reference_month=request.args.get("month") or date.today().strftime("%Y-%m"); start,end=month_bounds(reference_month)
    rows=Transaction.query.filter(Transaction.transaction_date.between(start,end)).order_by(Transaction.transaction_date).all()
    income=sum((Decimal(x.amount) for x in rows if x.kind=="income"),Decimal("0")); expenses=sum((Decimal(x.amount) for x in rows if x.kind=="expense"),Decimal("0"))
    lines=["GRANA - RELATORIO MENSAL",month_label(reference_month),"",f"Entradas: {brl(income)}",f"Despesas: {brl(expenses)}",f"Resultado: {brl(income-expenses)}","","MOVIMENTACOES"]
    lines.extend(f"{x.transaction_date:%d/%m} | {x.description[:48]} | {brl(x.amount)}" for x in rows[:42])
    return send_file(BytesIO(build_simple_pdf(lines)),mimetype="application/pdf",as_attachment=True,download_name=f"grana-relatorio-{reference_month}.pdf")


@main.get("/dados/backup.json")
@login_required
def backup_data():
    payload={"generated_at":datetime.utcnow().isoformat(),"accounts":[{"name":x.name,"institution":x.institution,"initial_balance":str(x.initial_balance)} for x in Account.query.all()],"categories":[{"name":x.name,"kind":x.kind,"parent":x.parent.name if x.parent else None} for x in Category.query.all()],"transactions":[{"description":x.description,"amount":str(x.amount),"kind":x.kind,"date":x.transaction_date.isoformat(),"category":x.category.full_name if x.category else None} for x in Transaction.query.all()]}
    data=BytesIO(json.dumps(payload,ensure_ascii=False,indent=2).encode("utf-8")); return send_file(data,mimetype="application/json",as_attachment=True,download_name=f"grana-backup-{date.today().isoformat()}.json")


@main.route("/dados", methods=["GET", "POST"])
@login_required
def data_tools():
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Selecione um arquivo CSV ou OFX.", "danger"); return redirect(url_for("main.data_tools"))
        try:
            content = file.read().decode("utf-8-sig", errors="ignore")
            imported = 0
            if file.filename.lower().endswith(".csv"):
                dialect = csv.Sniffer().sniff(content[:2048], delimiters=";,\t,")
                for row in csv.DictReader(StringIO(content), dialect=dialect):
                    lowered = {str(key).strip().casefold(): value for key,value in row.items()}
                    description = (lowered.get("descrição") or lowered.get("descricao") or lowered.get("description") or "Importado").strip()
                    raw_amount = (lowered.get("valor") or lowered.get("amount") or "0").replace("R$","").strip()
                    amount = Decimal(raw_amount.replace(".","").replace(",","."))
                    raw_date = lowered.get("data") or lowered.get("date")
                    parsed_date = datetime.strptime(raw_date, "%d/%m/%Y").date() if "/" in raw_date else datetime.strptime(raw_date, "%Y-%m-%d").date()
                    kind = lowered.get("tipo") or lowered.get("kind") or ("expense" if amount < 0 else "income")
                    db.session.add(Transaction(description=description,amount=abs(amount),kind="expense" if kind in {"expense","saída","saida"} else "income",transaction_date=parsed_date,category_id=automatic_category(description,"expense" if amount < 0 else "income"),source="csv")); imported += 1
            elif file.filename.lower().endswith(".ofx"):
                import re
                for block in re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>))",content,re.S|re.I):
                    def tag(name):
                        match=re.search(rf"<{name}>([^<\r\n]+)",block,re.I); return match.group(1).strip() if match else ""
                    amount=Decimal(tag("TRNAMT") or "0"); raw_date=tag("DTPOSTED")[:8]
                    description=tag("MEMO") or tag("NAME") or "Importado OFX"
                    db.session.add(Transaction(description=description,amount=abs(amount),kind="expense" if amount < 0 else "income",transaction_date=datetime.strptime(raw_date,"%Y%m%d").date(),category_id=automatic_category(description,"expense" if amount < 0 else "income"),source="ofx")); imported += 1
            else:
                raise ValueError
            db.session.commit(); flash(f"{imported} lançamentos importados. Revise as categorias.", "success")
        except Exception:
            db.session.rollback(); flash("Não foi possível interpretar o arquivo. Confira colunas, datas e valores.", "danger")
    return render_template("data_tools.html")


@main.post("/faturas/<int:invoice_id>/reprocessar")
@login_required
def reprocess_invoice(invoice_id):
    invoice = db.get_or_404(Invoice, invoice_id)
    if invoice.status != "draft":
        flash("Somente faturas aguardando revisão podem ser reprocessadas.", "warning")
        return redirect(url_for("main.invoices"))
    if not invoice.drive_file_id:
        flash("Esta fatura foi enviada manualmente. Descarte-a e envie o PDF novamente.", "warning")
        return redirect(url_for("main.review_invoice", invoice_id=invoice.id))

    try:
        cards = CreditCard.query.filter_by(active=True).all()
        drive_session, files = list_month_pdfs(invoice.reference_month)
        drive_file = next(
            (item for item in files if item["id"] == invoice.drive_file_id),
            None,
        )
        if not drive_file:
            raise DriveAccessError("O PDF não está mais na pasta desse mês no Google Drive.")
        downloaded = download_pdf(drive_session, invoice.drive_file_id).getvalue()
        parsed = parse_with_saved_passwords(
            lambda data=downloaded: BytesIO(data),
            invoice.reference_month,
            cards,
            invoice_provider_hint(invoice.card),
        )
        if not parsed["items"]:
            raise ValueError("Nenhuma compra foi encontrada no novo processamento.")
        if parsed.get("adapter") != invoice.card.invoice_provider:
            raise ValueError("O banco identificado não corresponde ao cartão deste rascunho.")
        replace_invoice_draft(invoice, parsed)
        invoice.original_filename = display_filename(drive_file["name"])
        db.session.commit()
        flash("Fatura processada novamente. Confira os lançamentos encontrados.", "success")
        return redirect(url_for("main.review_invoice", invoice_id=invoice.id))
    except (PdfPasswordRequired, PdfPasswordInvalid, DriveAccessError, DriveConfigurationError, ValueError) as exc:
        db.session.rollback()
        flash(str(exc) or "Não foi possível reprocessar a fatura.", "danger")
    except Exception:
        db.session.rollback()
        flash("Não foi possível reprocessar a fatura.", "danger")
    return redirect(url_for("main.invoices", status="draft"))


@main.route("/investimentos", methods=["GET", "POST"])
@login_required
def investments():
    if request.method == "POST":
        try:
            item = Investment(
                operation=request.form["operation"], category=request.form["category"].strip(),
                subcategory=request.form.get("subcategory", "").strip(), asset=request.form["asset"].strip().upper(),
                quantity=form_decimal("quantity"), unit_value=form_decimal("unit_value"), fees=form_decimal("fees") if request.form.get("fees") else Decimal("0"), benchmark=request.form.get("benchmark", "CDI"),
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
    positions = []
    for asset in sorted({row.asset for row in all_items if row.operation in {"Compra","Venda"}}):
        asset_rows = [row for row in all_items if row.asset == asset]
        buys = [row for row in asset_rows if row.operation == "Compra"]
        sales = [row for row in asset_rows if row.operation == "Venda"]
        quantity = sum((Decimal(row.quantity) for row in buys),Decimal("0"))-sum((Decimal(row.quantity) for row in sales),Decimal("0"))
        cost = sum((row.total_value + Decimal(row.fees or 0) for row in buys),Decimal("0"))
        average = cost / sum((Decimal(row.quantity) for row in buys),Decimal("0")) if buys else Decimal("0")
        quote = AssetPrice.query.filter_by(asset=asset).first(); current_price = Decimal(quote.current_price) if quote else average
        current_value = quantity * current_price; invested_cost = quantity * average; pnl = current_value-invested_cost
        receipts = sum((row.total_value for row in asset_rows if row.operation == "Recebimento"),Decimal("0"))
        positions.append({"asset":asset,"quantity":quantity,"average":average,"current_price":current_price,"current_value":current_value,"pnl":pnl,"return_pct":(pnl/invested_cost*100).quantize(Decimal("0.1")) if invested_cost else None,"receipts":receipts,"updated_at":quote.updated_at if quote else None})
    categories = [row[0] for row in db.session.query(Investment.category).distinct().order_by(Investment.category).all()]
    subcategories = [row[0] for row in db.session.query(Investment.subcategory).filter(Investment.subcategory != "").distinct().order_by(Investment.subcategory).all()]
    return render_template("investments.html", investments=items, positions=positions, today=date.today(), total_transferred=total_transferred, total_buys=total_buys, total_sales=total_sales, total_receipts=total_receipts, invested_value=invested_value, broker_balance=broker_balance, categories=categories, subcategories=subcategories, filters={"start":start,"end":end,"category":category,"subcategory":subcategory})


@main.post("/investimentos/cotacao")
@login_required
def update_asset_price():
    try:
        asset=request.form["asset"].strip().upper(); price=form_decimal("current_price")
        if not asset or price<=0: raise ValueError
        quote=AssetPrice.query.filter_by(asset=asset).first() or AssetPrice(asset=asset,current_price=price)
        quote.current_price=price; db.session.add(quote); db.session.commit(); flash("Cotação atualizada.", "success")
    except (ValueError,InvalidOperation):
        db.session.rollback(); flash("Informe ativo e cotação válidos.", "danger")
    return redirect(url_for("main.investments"))


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
            item.fees = form_decimal("fees") if request.form.get("fees") else Decimal("0")
            item.benchmark = request.form.get("benchmark", "CDI")
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
