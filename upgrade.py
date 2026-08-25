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
            "institution": "ALTER TABLE credit_card ADD COLUMN institution VARCHAR(100) DEFAULT '' NOT NULL",
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
            "payment_responsibility": "ALTER TABLE invoice_item ADD COLUMN payment_responsibility VARCHAR(20) DEFAULT 'self' NOT NULL",
            "personal_amount": "ALTER TABLE invoice_item ADD COLUMN personal_amount NUMERIC(12, 2)",
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
        "transaction": {
            "source": "ALTER TABLE \"transaction\" ADD COLUMN source VARCHAR(20) DEFAULT 'manual' NOT NULL",
            "status": "ALTER TABLE \"transaction\" ADD COLUMN status VARCHAR(20) DEFAULT 'confirmed' NOT NULL",
            "installment_current": "ALTER TABLE \"transaction\" ADD COLUMN installment_current INTEGER",
            "installment_total": "ALTER TABLE \"transaction\" ADD COLUMN installment_total INTEGER",
            "recurring_id": "ALTER TABLE \"transaction\" ADD COLUMN recurring_id INTEGER REFERENCES recurring_transaction(id)",
            "competence_month": "ALTER TABLE \"transaction\" ADD COLUMN competence_month VARCHAR(7)",
            "payment_responsibility": "ALTER TABLE \"transaction\" ADD COLUMN payment_responsibility VARCHAR(20) DEFAULT 'self' NOT NULL",
            "personal_amount": "ALTER TABLE \"transaction\" ADD COLUMN personal_amount NUMERIC(12, 2)",
        },
        "investment": {
            "fees": "ALTER TABLE investment ADD COLUMN fees NUMERIC(12, 2) DEFAULT 0 NOT NULL",
            "benchmark": "ALTER TABLE investment ADD COLUMN benchmark VARCHAR(20) DEFAULT 'CDI' NOT NULL",
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
    db.session.execute(text("UPDATE credit_card SET institution = CASE invoice_provider WHEN 'bb_smiles' THEN 'Banco do Brasil' WHEN 'mercado_pago' THEN 'Mercado Pago' WHEN 'banco_inter' THEN 'Banco Inter' WHEN 'itau' THEN 'Itaú' ELSE institution END WHERE institution IS NULL OR institution = ''"))
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
