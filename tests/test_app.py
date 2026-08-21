from datetime import date
from decimal import Decimal

import pytest

from app import create_app
from app.extensions import db
from app.models import Account, CardCycle, Category, CreditCard, Investment, Transaction, User
from app.services.invoice_parser import detect_due_date, parse_brl, parse_date


class TestConfig:
    TESTING = True
    SECRET_KEY = "test-key"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_ENABLED = False


@pytest.fixture()
def app():
    app = create_app(TestConfig)
    with app.app_context():
        user = User(username="guilherme"); user.set_password("senha-segura")
        db.session.add_all([user, Account(name="Principal", initial_balance=100), Category(name="Alimentação", kind="expense"), CreditCard(name="Platinum", last_digits="2616", credit_limit=5000, closing_day=20, due_day=28)])
        db.session.commit()
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client):
    return client.post("/login", data={"username": "guilherme", "password": "senha-segura"}, follow_redirects=True)


def test_login_and_dashboard(client):
    response = login(client)
    assert response.status_code == 200
    assert "Visão geral" in response.text
    assert "R$ 100,00" in response.text


def test_create_transaction(client, app):
    login(client)
    with app.app_context():
        account = Account.query.first(); category = Category.query.first()
        ids = (account.id, category.id)
    response = client.post("/movimentacoes", data={"description":"Mercado","amount":"50,90","kind":"expense","transaction_date":date.today().isoformat(),"account_id":ids[0],"category_id":ids[1]}, follow_redirects=True)
    assert response.status_code == 200
    assert "Mercado" in response.text
    with app.app_context():
        assert Transaction.query.count() == 1
        assert Transaction.query.first().amount == Decimal("50.90")


def test_money_and_date_parser():
    assert parse_brl("R$ 1.234,56") == Decimal("1234.56")
    assert parse_date("15/07", "2026-08") == date(2026, 7, 15)
    assert detect_due_date("Data de vencimento: 28/08/2026", "2026-08") == date(2026, 8, 28)


def test_monthly_card_cycle(client, app):
    login(client)
    with app.app_context():
        card_id = CreditCard.query.first().id
    response = client.post(f"/cartoes/{card_id}/configurar", data={
        "action":"cycle", "reference_month":"2026-08",
        "closing_date":"2026-08-20", "due_date":"2026-08-28",
    }, follow_redirects=True)
    assert response.status_code == 200
    assert "Datas específicas dessa fatura salvas" in response.text
    with app.app_context():
        assert CardCycle.query.filter_by(reference_month="2026-08").count() == 1


def test_create_investment(client, app):
    login(client)
    response = client.post("/investimentos", data={
        "operation":"Compra", "category":"Renda variável", "subcategory":"FII",
        "asset":"rzag11", "quantity":"10", "unit_value":"9,50",
        "operation_date":"2026-08-20",
    }, follow_redirects=True)
    assert response.status_code == 200
    assert "RZAG11" in response.text
    with app.app_context():
        assert Investment.query.first().total_value == Decimal("95.0000000000")
