"""Atualização segura e idempotente para instalações existentes."""

from app import create_app
from app.extensions import db
from app.models import Category
from sqlalchemy import inspect, text


app = create_app()
with app.app_context():
    db.create_all()
    inspector = inspect(db.engine)
    migrations = {
        "credit_card": {
            "invoice_provider": "ALTER TABLE credit_card ADD COLUMN invoice_provider VARCHAR(30) DEFAULT '' NOT NULL",
            "pdf_password_encrypted": "ALTER TABLE credit_card ADD COLUMN pdf_password_encrypted TEXT",
        },
        "invoice": {
            "source": "ALTER TABLE invoice ADD COLUMN source VARCHAR(30) DEFAULT 'generic' NOT NULL",
            "credit_limit": "ALTER TABLE invoice ADD COLUMN credit_limit NUMERIC(12, 2)",
            "cash_advance_total": "ALTER TABLE invoice ADD COLUMN cash_advance_total NUMERIC(12, 2)",
            "statement_total": "ALTER TABLE invoice ADD COLUMN statement_total NUMERIC(12, 2)",
            "suggested_closing_date": "ALTER TABLE invoice ADD COLUMN suggested_closing_date DATE",
            "suggested_due_date": "ALTER TABLE invoice ADD COLUMN suggested_due_date DATE",
            "date_source": "ALTER TABLE invoice ADD COLUMN date_source VARCHAR(20) DEFAULT 'default' NOT NULL",
            "drive_file_id": "ALTER TABLE invoice ADD COLUMN drive_file_id VARCHAR(255)",
        },
        "invoice_item": {
            "installment_current": "ALTER TABLE invoice_item ADD COLUMN installment_current INTEGER",
            "installment_total": "ALTER TABLE invoice_item ADD COLUMN installment_total INTEGER",
        },
        "category": {
            "parent_id": "ALTER TABLE category ADD COLUMN parent_id INTEGER REFERENCES category(id)",
            "active": "ALTER TABLE category ADD COLUMN active BOOLEAN DEFAULT 1 NOT NULL",
            "necessity": "ALTER TABLE category ADD COLUMN necessity VARCHAR(20) DEFAULT 'essential' NOT NULL",
            "frequency": "ALTER TABLE category ADD COLUMN frequency VARCHAR(20) DEFAULT 'variable' NOT NULL",
            "monthly_budget": "ALTER TABLE category ADD COLUMN monthly_budget NUMERIC(12, 2)",
            "protected": "ALTER TABLE category ADD COLUMN protected BOOLEAN DEFAULT 0 NOT NULL",
            "sort_order": "ALTER TABLE category ADD COLUMN sort_order INTEGER DEFAULT 0 NOT NULL",
        },
    }
    for table_name, columns in migrations.items():
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name, statement in columns.items():
            if column_name not in existing:
                db.session.execute(text(statement))
    db.session.commit()
    db.session.execute(text(
        "UPDATE invoice SET statement_total = total "
        "WHERE statement_total IS NULL AND status = 'draft'"
    ))
    db.session.commit()
    db.session.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_invoice_drive_file_id "
        "ON invoice (drive_file_id) WHERE drive_file_id IS NOT NULL"
    ))
    db.session.commit()
    investment_category = Category.query.filter(
        db.func.lower(Category.name).in_(["investimento", "investimentos"])
    ).first()
    if not investment_category:
        db.session.add(
            Category(
                name="Investimentos",
                kind="expense",
                color="#D8B56A",
                icon="↗",
            )
        )
        db.session.commit()
    print("Banco atualizado com sucesso.")
