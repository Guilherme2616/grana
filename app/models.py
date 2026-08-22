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

    account = db.relationship("Account", backref="transactions")
    category = db.relationship("Category", backref="transactions")
    card = db.relationship("CreditCard", backref="transactions")


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

    category = db.relationship("Category")


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
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def total_value(self):
        return (self.quantity or Decimal("0")) * (self.unit_value or Decimal("0"))
