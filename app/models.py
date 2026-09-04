from datetime import date, datetime
from decimal import Decimal

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Account(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    institution = db.Column(db.String(100), default="")
    account_type = db.Column(db.String(30), default="corrente")
    initial_balance = db.Column(db.Numeric(12, 2), default=Decimal("0.00"))
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    kind = db.Column(db.String(10), nullable=False, default="expense")
    color = db.Column(db.String(10), default="#D8B56A")
    icon = db.Column(db.String(10), default="$" )
    parent_id = db.Column(db.Integer, db.ForeignKey("category.id"), nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    necessity = db.Column(db.String(20), default="essential", nullable=False)
    frequency = db.Column(db.String(20), default="variable", nullable=False)
    monthly_budget = db.Column(db.Numeric(12, 2), nullable=True)
    protected = db.Column(db.Boolean, default=False, nullable=False)
    sort_order = db.Column(db.Integer, default=0, nullable=False)

    parent = db.relationship("Category", remote_side=[id], backref=db.backref("children", lazy="dynamic"))

    @property
    def full_name(self):
        return f"{self.parent.name} › {self.name}" if self.parent else self.name


class CategoryRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pattern = db.Column(db.String(120), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"), nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    category = db.relationship("Category", backref=db.backref("rules", cascade="all, delete-orphan"))


class CreditCard(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    last_digits = db.Column(db.String(4), default="0000")
    credit_limit = db.Column(db.Numeric(12, 2), default=Decimal("0.00"))
    closing_day = db.Column(db.Integer, nullable=False)
    due_day = db.Column(db.Integer, nullable=False)
    color = db.Column(db.String(10), default="#173F35")
    invoice_provider = db.Column(db.String(30), default="", nullable=False)
    pdf_password_encrypted = db.Column(db.Text, nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    institution = db.Column(db.String(100), default="", nullable=False)


class CardCycle(db.Model):
    __table_args__ = (
        db.UniqueConstraint("card_id", "reference_month", name="uq_card_cycle_month"),
    )

    id = db.Column(db.Integer, primary_key=True)
    card_id = db.Column(db.Integer, db.ForeignKey("credit_card.id"), nullable=False)
    reference_month = db.Column(db.String(7), nullable=False)
    closing_date = db.Column(db.Date, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    source = db.Column(db.String(20), default="manual", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    card = db.relationship("CreditCard", backref=db.backref("cycles", cascade="all, delete-orphan"))


class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(180), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    kind = db.Column(db.String(10), nullable=False)
    transaction_date = db.Column(db.Date, nullable=False, default=date.today)
    notes = db.Column(db.Text, default="")
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=True)
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"), nullable=True)
    card_id = db.Column(db.Integer, db.ForeignKey("credit_card.id"), nullable=True)
    invoice_item_id = db.Column(db.Integer, db.ForeignKey("invoice_item.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    source = db.Column(db.String(20), default="manual", nullable=False)
    status = db.Column(db.String(20), default="confirmed", nullable=False)
    installment_current = db.Column(db.Integer, nullable=True)
    installment_total = db.Column(db.Integer, nullable=True)
    recurring_id = db.Column(db.Integer, db.ForeignKey("recurring_transaction.id"), nullable=True)
    competence_month = db.Column(db.String(7), nullable=True)
    payment_responsibility = db.Column(db.String(20), default="self", nullable=False)
    personal_amount = db.Column(db.Numeric(12, 2), nullable=True)

    account = db.relationship("Account", backref="transactions")
    category = db.relationship("Category", backref="transactions")
    card = db.relationship("CreditCard", backref="transactions")


class TransactionSplit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(db.Integer, db.ForeignKey("transaction.id"), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)

    transaction = db.relationship("Transaction", backref=db.backref("splits", cascade="all, delete-orphan"))
    category = db.relationship("Category")


class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    card_id = db.Column(db.Integer, db.ForeignKey("credit_card.id"), nullable=False)
    reference_month = db.Column(db.String(7), nullable=False)
    total = db.Column(db.Numeric(12, 2), default=Decimal("0.00"))
    status = db.Column(db.String(20), default="draft", nullable=False)
    original_filename = db.Column(db.String(255), default="")
    source = db.Column(db.String(30), default="generic", nullable=False)
    credit_limit = db.Column(db.Numeric(12, 2), nullable=True)
    cash_advance_total = db.Column(db.Numeric(12, 2), nullable=True)
    statement_total = db.Column(db.Numeric(12, 2), nullable=True)
    suggested_closing_date = db.Column(db.Date, nullable=True)
    suggested_due_date = db.Column(db.Date, nullable=True)
    date_source = db.Column(db.String(20), default="default", nullable=False)
    drive_file_id = db.Column(db.String(255), unique=True, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    card = db.relationship("CreditCard", backref="invoices")
    items = db.relationship("InvoiceItem", backref="invoice", cascade="all, delete-orphan")


class InvoiceItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False)
    purchase_date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(180), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    installment_current = db.Column(db.Integer, nullable=True)
    installment_total = db.Column(db.Integer, nullable=True)
    selected = db.Column(db.Boolean, default=True, nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"), nullable=True)
    payment_responsibility = db.Column(db.String(20), default="self", nullable=False)
    personal_amount = db.Column(db.Numeric(12, 2), nullable=True)

    category = db.relationship("Category")


class InvoicePayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    payment_date = db.Column(db.Date, nullable=False, default=date.today)
    paid_by = db.Column(db.String(20), default="self", nullable=False)
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    invoice = db.relationship("Invoice", backref=db.backref("payments", cascade="all, delete-orphan"))
    account = db.relationship("Account", backref="invoice_payments")


class Investment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    operation = db.Column(db.String(20), nullable=False)
    category = db.Column(db.String(60), nullable=False)
    subcategory = db.Column(db.String(60), default="")
    asset = db.Column(db.String(30), nullable=False)
    quantity = db.Column(db.Numeric(18, 8), nullable=False, default=Decimal("0"))
    unit_value = db.Column(db.Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    operation_date = db.Column(db.Date, nullable=False, default=date.today)
    notes = db.Column(db.Text, default="")
    fees = db.Column(db.Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    benchmark = db.Column(db.String(20), default="CDI", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def total_value(self):
        return (self.quantity or Decimal("0")) * (self.unit_value or Decimal("0"))


class DividendImport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original_filename = db.Column(db.String(255), nullable=False)
    drive_file_id = db.Column(db.String(255), unique=True, nullable=True)
    period_start = db.Column(db.Date, nullable=True)
    period_end = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(20), default="draft", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    confirmed_at = db.Column(db.DateTime, nullable=True)


class DividendIncome(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    import_id = db.Column(db.Integer, db.ForeignKey("dividend_import.id"), nullable=False)
    income_type = db.Column(db.String(50), nullable=False)
    asset = db.Column(db.String(30), nullable=False)
    asset_name = db.Column(db.String(180), default="", nullable=False)
    institution = db.Column(db.String(140), default="", nullable=False)
    quantity = db.Column(db.Numeric(18, 8), nullable=False)
    unit_value = db.Column(db.Numeric(18, 8), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    payment_date = db.Column(db.Date, nullable=False)
    selected = db.Column(db.Boolean, default=True, nullable=False)
    fingerprint = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    source_import = db.relationship(
        "DividendImport",
        backref=db.backref("items", cascade="all, delete-orphan", lazy=True),
    )


class AssetPrice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    asset = db.Column(db.String(30), unique=True, nullable=False)
    current_price = db.Column(db.Numeric(14, 2), nullable=False)
    asset_name = db.Column(db.String(120), default="", nullable=False)
    change_percent = db.Column(db.Numeric(10, 4), nullable=True)
    source = db.Column(db.String(30), default="manual", nullable=False)
    last_attempt_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class IntegrationSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(60), unique=True, nullable=False)
    encrypted_value = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class RecurringTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(180), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    kind = db.Column(db.String(10), nullable=False, default="expense")
    frequency = db.Column(db.String(20), nullable=False, default="monthly")
    day = db.Column(db.Integer, nullable=False, default=1)
    start_date = db.Column(db.Date, nullable=False, default=date.today)
    end_date = db.Column(db.Date, nullable=True)
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"), nullable=True)
    account_id = db.Column(db.Integer, db.ForeignKey("account.id"), nullable=True)
    card_id = db.Column(db.Integer, db.ForeignKey("credit_card.id"), nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    auto_create = db.Column(db.Boolean, default=False, nullable=False)
    notes = db.Column(db.Text, default="")

    category = db.relationship("Category")
    account = db.relationship("Account")
    card = db.relationship("CreditCard")


class FinancialGoal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    target_amount = db.Column(db.Numeric(12, 2), nullable=False)
    current_amount = db.Column(db.Numeric(12, 2), default=Decimal("0.00"), nullable=False)
    target_date = db.Column(db.Date, nullable=True)
    color = db.Column(db.String(10), default="#D8B56A")
    active = db.Column(db.Boolean, default=True, nullable=False)
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def progress(self):
        if not self.target_amount:
            return Decimal("0")
        return min(Decimal("100"), (Decimal(self.current_amount) / Decimal(self.target_amount) * 100).quantize(Decimal("0.1")))


class MonthlyClose(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reference_month = db.Column(db.String(7), unique=True, nullable=False)
    income = db.Column(db.Numeric(12, 2), nullable=False)
    expenses = db.Column(db.Numeric(12, 2), nullable=False)
    balance = db.Column(db.Numeric(12, 2), nullable=False)
    snapshot_json = db.Column(db.Text, default="{}", nullable=False)
    closed_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
