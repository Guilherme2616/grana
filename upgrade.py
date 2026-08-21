"""Atualização segura e idempotente para instalações existentes."""

from app import create_app
from app.extensions import db
from app.models import Category


app = create_app()
with app.app_context():
    db.create_all()
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
