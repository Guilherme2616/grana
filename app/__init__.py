from datetime import date
from decimal import Decimal

from flask import Flask
from flask_login import current_user
from sqlalchemy import func

from config import Config
from .extensions import csrf, db, login_manager
from .models import User


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    login_manager.login_view = "main.login"
    login_manager.login_message = "Entre para acessar seu financeiro."
    login_manager.login_message_category = "warning"

    from .routes import main
    app.register_blueprint(main)

    with app.app_context():
        db.create_all()

    @app.context_processor
    def navigation_counts():
        if not current_user.is_authenticated:
            return {"nav_pending_invoices": 0, "nav_alerts": 0}
        from .models import Category, CreditCard, Invoice, Transaction
        pending = Invoice.query.filter_by(status="draft").count()
        alert_count = Transaction.query.filter(Transaction.status == "planned", Transaction.transaction_date <= date.today()).count()
        for card in CreditCard.query.filter_by(active=True).all():
            used = db.session.query(func.sum(Transaction.amount)).filter_by(card_id=card.id, kind="expense").scalar() or 0
            if card.credit_limit and used / card.credit_limit >= Decimal("0.8"):
                alert_count += 1
        for category in Category.query.filter(Category.active.is_(True), Category.monthly_budget.isnot(None)).all():
            start = date.today().replace(day=1)
            spent = db.session.query(func.sum(Transaction.amount)).filter(Transaction.category_id == category.id, Transaction.kind == "expense", Transaction.transaction_date >= start).scalar() or 0
            if category.monthly_budget and spent >= category.monthly_budget * Decimal("0.9"):
                alert_count += 1
        return {"nav_pending_invoices": pending, "nav_alerts": alert_count}

    return app


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))
