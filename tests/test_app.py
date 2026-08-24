from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest

from app import create_app
from app.extensions import db
from app.models import Account, CardCycle, Category, CategoryRule, CreditCard, FinancialGoal, Investment, Invoice, InvoiceItem, MonthlyClose, RecurringTransaction, Transaction, TransactionSplit, User
from app.services.drive_sync import month_folder_matches, normalize_folder_name
from app.services.financial_analytics import build_installment_projection, month_label, shift_month
from app.services.invoice_parser import (
    detect_bb_statement_total,
    detect_due_date,
    is_banco_inter_invoice,
    is_bb_smiles_invoice,
    is_itau_invoice,
    is_mercado_pago_invoice,
    parse_bb_smiles_text,
    parse_banco_inter_summary,
    parse_banco_inter_text,
    parse_brl,
    parse_date,
    parse_mercado_pago_summary,
    parse_mercado_pago_text,
    parse_itau_summary,
    parse_itau_text,
)
from app.services.secret_store import decrypt_secret


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


def test_category_hierarchy_rule_edit_and_safe_delete(client, app):
    login(client)
    with app.app_context():
        root = Category.query.filter_by(name="Alimentação").first()
        root_id = root.id
    response = client.post("/categorias", data={"name":"Delivery","kind":"expense","parent_id":root_id,"necessity":"nonessential","frequency":"variable","monthly_budget":"200,00","color":"#123456","icon":"D"}, follow_redirects=True)
    assert "Delivery" in response.text
    with app.app_context():
        child = Category.query.filter_by(name="Delivery").first(); child_id = child.id
        assert child.parent_id == root_id
        assert child.monthly_budget == Decimal("200.00")
    client.post("/categorias/regras", data={"pattern":"IFOOD","category_id":child_id}, follow_redirects=True)
    client.post("/movimentacoes", data={"description":"IFOOD PEDIDO","amount":"42,00","kind":"expense","transaction_date":date.today().isoformat()}, follow_redirects=True)
    with app.app_context():
        assert Transaction.query.first().category_id == child_id
        assert CategoryRule.query.filter_by(pattern="IFOOD").count() == 1
    response = client.post(f"/categorias/{child_id}/excluir", data={"replacement_id":root_id,"confirm_reassign":"yes"}, follow_redirects=True)
    assert "histórico preservado" in response.text
    with app.app_context():
        assert Category.query.filter_by(id=child_id).first() is None
        assert Transaction.query.first().category_id == root_id


def test_split_transaction_requires_exact_total(client, app):
    login(client)
    with app.app_context():
        first = Category.query.first()
        second = Category(name="Higiene", kind="expense")
        transaction = Transaction(description="Compra mista", amount=Decimal("100"), kind="expense", transaction_date=date.today(), category_id=first.id)
        db.session.add_all([second, transaction]); db.session.commit()
        ids = (transaction.id, first.id, second.id)
    client.post(f"/movimentacoes/{ids[0]}/dividir", data={"split_category_id":[ids[1],ids[2]],"split_amount":["70,00","30,00"]}, follow_redirects=True)
    with app.app_context():
        assert TransactionSplit.query.filter_by(transaction_id=ids[0]).count() == 2
        assert Transaction.query.get(ids[0]).category_id is None


def test_indicator_filters_card_bank_and_subcategory(client, app):
    login(client)
    with app.app_context():
        root=Category.query.first(); child=Category(name="Restaurante",kind="expense",parent_id=root.id)
        card=CreditCard.query.first(); card.institution="Banco Teste"
        db.session.add(child); db.session.flush()
        db.session.add_all([Transaction(description="Jantar",amount=90,kind="expense",transaction_date=date.today(),category_id=child.id,card_id=card.id),Transaction(description="Outro",amount=200,kind="expense",transaction_date=date.today())]);db.session.commit(); ids=(child.id,card.id)
    response=client.get(f"/indicadores?subcategory_id={ids[0]}&card_id={ids[1]}&institution=Banco+Teste")
    assert response.status_code==200
    assert "R$ 90,00" in response.text


def test_recurring_calendar_goal_closing_and_export(client, app):
    login(client)
    today=date.today()
    response=client.post("/recorrencias",data={"description":"Academia","amount":"100,00","kind":"expense","frequency":"monthly","day":today.day,"start_date":today.isoformat(),"auto_create":"on"},follow_redirects=True)
    assert "Academia" in response.text
    response=client.get(f"/calendario?month={today:%Y-%m}")
    assert "Academia" in response.text
    client.post("/metas",data={"name":"Reserva","target_amount":"1000,00","current_amount":"100,00"})
    with app.app_context():
        assert FinancialGoal.query.filter_by(name="Reserva").first().progress==Decimal("10.0")
        assert RecurringTransaction.query.count()==1
    client.post("/fechamento",data={"month":f"{today:%Y-%m}","action":"close"})
    with app.app_context(): assert MonthlyClose.query.count()==1
    export=client.get("/dados/exportar.csv")
    assert export.status_code==200 and "text/csv" in export.content_type


def test_money_and_date_parser():
    assert parse_brl("R$ 1.234,56") == Decimal("1234.56")
    assert parse_brl("R$ 661,26-") == Decimal("-661.26")
    assert parse_date("15/07", "2026-08") == date(2026, 7, 15)
    assert detect_due_date("Data de vencimento: 28/08/2026", "2026-08") == date(2026, 8, 28)
    assert detect_due_date("Vencimento\n10/08/2026", "2026-08") == date(2026, 8, 10)


def test_bb_smiles_adapter_respects_table_boundaries_and_columns():
    extracted_text = """
    Banco do Brasil
    SMILES PLATINUM VISA
    Olá, sua fatura de AGOSTO
    Valor
    R$ 294,31
    Vencimento 10/08/2026

    Lançamentos nesta fatura
    Data Descrição País Valor
    Pagamentos
    06/07 PGTO. CASH AG. 0470 000047000 200 10 R$ 661,26-
    Compras diversas
    07/07 APPLE.COM/BILL SAO PAULO BR R$ 66,90
    13/07 TOTALPASS SAO PAULO BR R$ 59,90
    15/07 Smiles Clube Smiles Barueri BR R$ 46,00
    29/04 GOL LINHAS A* PARC 03/05 SAO PAULO BR R$ 30,80
    23/07 CLUBE LIVELO* PARC 01/12 SANTANA DE PA BR R$ 42,71
    28/07 ANUIDADE DIFERENCIADA TIT-PARC 05/12 BR R$ 48,00
    Subtotal R$ 294,31
    Total R$ 294,31
    Parcelamentos Próxima Fatura
    29/04 GOL LINHAS A* PARC 04/05 SAO PAULO BR R$ 30,80
    """
    assert is_bb_smiles_invoice(extracted_text)
    assert detect_bb_statement_total(extracted_text) == Decimal("294.31")
    items = parse_bb_smiles_text(extracted_text, "2026-08")
    assert len(items) == 6
    assert items[0]["description"] == "APPLE.COM/BILL SAO PAULO"
    assert items[-1]["description"] == "ANUIDADE DIFERENCIADA TIT-PARC 05/12"
    assert items[3]["installment_current"] == 3
    assert items[3]["installment_total"] == 5
    assert sum(item["amount"] for item in items) == Decimal("294.31")
    assert all("PARC 04/05" not in item["description"] for item in items)


def test_bb_smiles_adapter_accepts_alternate_heading_and_country_after_amount():
    extracted_text = """
    BB.COM.BR
    CARTÃO SMILES PLATINUM
    Valor R$ 108,90
    Vencimento 10/08/2026
    Lançamentos e compras desta fatura
    Data Descrição País Valor
    07/07 APPLE.COM/BILL SAO PAULO R$ 66,90 BR
    23/07 CLUBE LIVELO* PARC 01/12 R$ 42,00 BR
    Total R$ 108,90
    Parcelamentos Próxima Fatura
    23/07 CLUBE LIVELO* PARC 02/12 R$ 42,00 BR
    """
    assert is_bb_smiles_invoice(extracted_text)
    items = parse_bb_smiles_text(extracted_text, "2026-08")
    assert len(items) == 2
    assert items[0]["description"] == "APPLE.COM/BILL SAO PAULO"
    assert items[1]["installment_current"] == 1
    assert items[1]["installment_total"] == 12
    assert sum(item["amount"] for item in items) == Decimal("108.90")


def test_mercado_pago_adapter_ignores_payments_and_reads_installments():
    cover = """
    mercado pago
    Essa é sua fatura de agosto
    Total a pagar     Vence em     Limite total     Saque total
    R$ 35,50          17/08/2026   R$ 5.100,00      R$ 50,00
    """
    details = """
    Detalhes de consumo
    Movimentações na fatura
    16/07 Pagamento da fatura de julho/2026 R$ 275,58
    05/08 Pagamento da fatura de agosto/2026 R$ 272,31
    Cartão Visa [************0563]
    Data Movimentações Valor em R$
    01/12 MERCADOLIVRE 2PRODUTOS Parcela 9 de 13 R$ 46,07
    01/12 MERCADOLIVRE*2PRODUTOS Parcela 9 de 10 R$ 3,64
    16/04 MERCADOLIVRE*MERCADOLI Parcela 4 de 4 R$ 60,05
    12/06 MERCADOLIVRE*MERCADOLIVRE Parcela 2 de 4 R$ 54,30
    02/07 MERCADOLIVRE*MERCADOLIVRE Parcela 2 de 10 R$ 47,58
    06/07 MERCADOLIVRE*MERCADOLI Parcela 2 de 9 R$ 40,44
    26/07 MP*GUILHERMEPAUL R$ 12,78
    04/08 MP*GUILHERMEPAUL R$ 7,45
    05/08 MERCADOLIVRE*CAELSTORE Parcela 1 de 4 R$ 35,50
    Total R$ 307,81
    06/08 ESTE LANCAMENTO NAO ENTRA R$ 99,99
    """
    assert is_mercado_pago_invoice(cover + details)
    summary = parse_mercado_pago_summary(cover, "2026-08")
    assert summary["due_date"] == date(2026, 8, 17)
    assert summary["statement_total"] == Decimal("35.50")
    assert summary["credit_limit"] == Decimal("5100.00")
    assert summary["cash_advance_total"] == Decimal("50.00")
    items = parse_mercado_pago_text(details, "2026-08")
    assert len(items) == 9
    assert sum(item["amount"] for item in items) == Decimal("307.81")
    assert items[0]["description"] == "MERCADOLIVRE 2PRODUTOS"
    assert items[0]["installment_current"] == 9
    assert items[0]["installment_total"] == 13
    assert all("Pagamento da fatura" not in item["description"] for item in items)
    assert all("NAO ENTRA" not in item["description"] for item in items)


def test_banco_inter_adapter_uses_expenses_and_ignores_green_credits():
    cover = """
    inter
    Resumo da fatura
    Total da sua fatura R$ 0,00
    Limite de crédito total R$ 3.500,00
    Data de Vencimento 12/08/2026
    """
    anticipation = """
    DESPESAS DO MÊS
    R$ 500,00
    VALOR ANTECIPADO R$ 500,00
    FATURA ATUAL R$ 0,00
    """
    details = """
    Despesas da fatura
    CARTÃO 2306****3146
    Data Movimentação Beneficiário Valor
    02 de jun. 2026 JIM.COM* LAURA LOTT D (Parcela 03 de 10) - R$ 500,00
    07 de jul. 2026 PAGAMENTO ON LINE - + R$ 500,00
    05 de ago. 2026 PAGAMENTO ON LINE - + R$ 500,00
    Total CARTÃO 2306****3146 R$ 500,00
    08 de ago. 2026 NAO DEVE ENTRAR R$ 99,99
    """
    assert is_banco_inter_invoice(cover + anticipation + details)
    summary = parse_banco_inter_summary(cover, anticipation, "2026-08")
    assert summary["due_date"] == date(2026, 8, 12)
    assert summary["statement_total"] == Decimal("500.00")
    assert summary["credit_limit"] == Decimal("3500.00")
    items = parse_banco_inter_text(details, "2026-08")
    assert len(items) == 1
    assert items[0]["purchase_date"] == date(2026, 6, 2)
    assert items[0]["description"] == "JIM.COM* LAURA LOTT D"
    assert items[0]["amount"] == Decimal("500.00")
    assert items[0]["installment_current"] == 3
    assert items[0]["installment_total"] == 10


def test_itau_adapter_reads_side_by_side_rows_and_reconciles_totals():
    cover = """
    itaú Personnalité
    O total da sua fatura é: R$ 2.363,58
    Com vencimento em: 10/08/2026
    Limite total de crédito: R$ 15.000,00
    """
    page_two = """
    Pagamentos efetuados
    10/07 Pagamento via conta -2.788,94
    LANÇAMENTOS: COMPRAS E SAQUES
    DATA ESTABELECIMENTO VALOR EM R$
    28/11 GRUPO CASAS B 09/10 30,89        05/07 ITAU NA BEATRIZ GERAL DOAD 38,13
    23/12 IEV ADAMANTINA 57,50              07/07 ALTO POSTO CARCERODADAM 50,00
    """
    page_three = """
    Lançamentos: compras e saques
    28/07 SUPERMERCADO RAVAZIADAM 11,84
    28/07 ALTO POSTO CARCERODADAM 50,00
    Lançamentos no cartão 2.197,72
    Lançamentos: produtos e serviços
    02/07 Mensalidade - Plano do Anuidade Diferenciada 88,00
    Lançamentos produtos e serviços 88,00
    Total dos lançamentos atuais 2.285,72
    Compras parceladas - próximas faturas
    28/11 GRUPO CASAS B 10/10 30,89
    Total de encargos em R$ 77,86
    """
    assert is_itau_invoice(cover + page_two + page_three)
    summary = parse_itau_summary(cover, page_three, "2026-08")
    assert summary["due_date"] == date(2026, 8, 10)
    assert summary["credit_limit"] == Decimal("15000.00")
    assert summary["components"] == (
        Decimal("2197.72"), Decimal("88.00"), Decimal("77.86")
    )
    assert summary["statement_total"] == Decimal("2363.58")
    assert summary["statement_total"] == summary["cover_total"]
    items = parse_itau_text(page_two + "\n" + page_three, "2026-08")
    assert len(items) == 7
    assert items[0]["description"] == "GRUPO CASAS B 09/10"
    assert items[1]["description"] == "ITAU NA BEATRIZ GERAL DOAD"
    assert items[0]["installment_current"] == 9
    assert items[0]["installment_total"] == 10
    assert all("Pagamento" not in item["description"] for item in items)
    assert all("10/10" not in item["description"] for item in items)


def test_drive_folder_names_and_encrypted_card_password(client, app):
    assert normalize_folder_name("08 - AGOSTO") == "08 AGOSTO"
    assert month_folder_matches("AGOSTO", 8)
    assert month_folder_matches("08 - Agosto", 8)
    assert not month_folder_matches("SETEMBRO", 8)

    login(client)
    with app.app_context():
        card = CreditCard.query.first()
        card_id = card.id
    response = client.post(f"/cartoes/{card_id}/configurar", data={
        "action": "card", "name": "Platinum", "last_digits": "2616",
        "credit_limit": "5000,00", "closing_day": "20", "due_day": "28",
        "color": "#173F35", "invoice_provider": "bb_smiles",
        "pdf_password": "senha-do-pdf",
    }, follow_redirects=True)
    assert response.status_code == 200
    with app.app_context():
        card = db.session.get(CreditCard, card_id)
        assert card.invoice_provider == "bb_smiles"
        assert card.pdf_password_encrypted != "senha-do-pdf"
        assert decrypt_secret(card.pdf_password_encrypted) == "senha-do-pdf"


def test_import_page_shows_drive_sync(client):
    login(client)
    response = client.get("/faturas/importar")
    assert response.status_code == 200
    assert "Sincronizar Google Drive" in response.text


def test_drive_sync_creates_draft_and_recovers_pending_review(client, app, monkeypatch):
    login(client)
    with app.app_context():
        card = CreditCard.query.first()
        card.invoice_provider = "bb_smiles"
        db.session.commit()

    fake_file = {"id": "drive-file-123", "name": "Banco do Brasil - Smiles.pdf"}
    parsed = {
        "items": [{
            "purchase_date": date(2026, 8, 5), "description": "COMPRA TESTE",
            "amount": Decimal("25.00"), "installment_current": None,
            "installment_total": None,
        }],
        "due_date": date(2026, 8, 10), "statement_total": Decimal("25.00"),
        "reference_month": "2026-08", "adapter": "bb_smiles",
        "credit_limit": None, "cash_advance_total": None,
    }
    monkeypatch.setattr("app.routes.list_month_pdfs", lambda month: (object(), [fake_file]))
    monkeypatch.setattr("app.routes.download_pdf", lambda drive_session, file_id: BytesIO(b"pdf"))
    monkeypatch.setattr("app.routes.parse_with_saved_passwords", lambda factory, month, cards: parsed)

    response = client.post("/faturas/sincronizar-drive", data={"reference_month": "2026-08"})
    assert response.status_code == 200
    assert "Pronta para revisar" in response.text
    with app.app_context():
        assert Invoice.query.filter_by(drive_file_id="drive-file-123", status="draft").count() == 1

    response = client.post("/faturas/sincronizar-drive", data={"reference_month": "2026-08"})
    assert response.status_code == 200
    assert "Aguardando revisão" in response.text
    assert "Continue de onde parou" in response.text
    with app.app_context():
        assert Invoice.query.filter_by(drive_file_id="drive-file-123").count() == 1

    pending = client.get("/faturas?status=draft")
    assert pending.status_code == 200
    assert "Banco do Brasil - Smiles.pdf" in pending.text
    assert "Continuar" in pending.text

    with app.app_context():
        invoice = Invoice.query.filter_by(drive_file_id="drive-file-123").first()
        invoice_id = invoice.id
        assert invoice.suggested_due_date == date(2026, 8, 10)
        assert invoice.date_source == "pdf"

    with client.session_transaction() as browser_session:
        browser_session.clear()
    login(client)
    review = client.get(f"/faturas/{invoice_id}/revisar")
    assert review.status_code == 200
    assert 'value="2026-08-10"' in review.text


def test_discard_pending_invoice_releases_drive_file(client, app):
    login(client)
    with app.app_context():
        card = CreditCard.query.first()
        invoice = Invoice(
            card_id=card.id,
            reference_month="2026-08",
            total=Decimal("25.00"),
            statement_total=Decimal("25.00"),
            status="draft",
            original_filename="fatura.pdf",
            drive_file_id="drive-file-to-discard",
        )
        db.session.add(invoice)
        db.session.commit()
        invoice_id = invoice.id

    response = client.post(f"/faturas/{invoice_id}/descartar", follow_redirects=True)
    assert response.status_code == 200
    assert "já pode ser importada novamente" in response.text
    with app.app_context():
        assert db.session.get(Invoice, invoice_id) is None
        assert Invoice.query.filter_by(drive_file_id="drive-file-to-discard").count() == 0


def test_reprocess_drive_draft_replaces_items(client, app, monkeypatch):
    login(client)
    with app.app_context():
        card = CreditCard.query.first()
        card.invoice_provider = "bb_smiles"
        invoice = Invoice(
            card_id=card.id,
            reference_month="2026-08",
            total=Decimal("10.00"),
            statement_total=Decimal("10.00"),
            status="draft",
            original_filename="fatura.pdf",
            drive_file_id="drive-file-reprocess",
        )
        db.session.add(invoice)
        db.session.flush()
        db.session.add(InvoiceItem(
            invoice_id=invoice.id,
            purchase_date=date(2026, 8, 1),
            description="ITEM ANTIGO",
            amount=Decimal("10.00"),
        ))
        db.session.commit()
        invoice_id = invoice.id

    parsed = {
        "items": [{
            "purchase_date": date(2026, 8, 5),
            "description": "ITEM CORRIGIDO",
            "amount": Decimal("30.00"),
            "installment_current": None,
            "installment_total": None,
        }],
        "due_date": date(2026, 8, 10),
        "statement_total": Decimal("30.00"),
        "reference_month": "2026-08",
        "adapter": "bb_smiles",
        "credit_limit": None,
        "cash_advance_total": None,
    }
    monkeypatch.setattr(
        "app.routes.list_month_pdfs",
        lambda month: (object(), [{"id": "drive-file-reprocess", "name": "BB Smiles.pdf"}]),
    )
    monkeypatch.setattr("app.routes.download_pdf", lambda drive_session, file_id: BytesIO(b"pdf"))
    monkeypatch.setattr("app.routes.parse_with_saved_passwords", lambda factory, month, cards: parsed)

    response = client.post(f"/faturas/{invoice_id}/reprocessar", follow_redirects=True)
    assert response.status_code == 200
    assert "Fatura processada novamente" in response.text
    with app.app_context():
        invoice = db.session.get(Invoice, invoice_id)
        assert invoice.total == Decimal("30.00")
        assert invoice.items[0].description == "ITEM CORRIGIDO"
        assert len(invoice.items) == 1


def test_financial_indicators_provisioning_and_simulator_pages(client, app):
    login(client)
    with app.app_context():
        card = CreditCard.query.first()
        invoice = Invoice(card_id=card.id, reference_month="2026-08", total=Decimal("100.00"), status="confirmed")
        db.session.add(invoice); db.session.flush()
        item = InvoiceItem(
            invoice_id=invoice.id, purchase_date=date(2026, 8, 2), description="NOTEBOOK PARC 03/10",
            amount=Decimal("100.00"), installment_current=3, installment_total=10, selected=True,
        )
        db.session.add(item); db.session.commit()
        projection = build_installment_projection([item], "2026-08", 12)
        assert projection["2026-09"][0]["installment_current"] == 4
        assert projection["2027-03"][0]["installment_current"] == 10
        assert shift_month("2026-12", 1) == "2027-01"
        assert month_label("2026-08") == "Agosto/2026"

    indicators = client.get("/indicadores?month=2026-08")
    assert indicators.status_code == 200
    assert "Indicadores financeiros" in indicators.text
    assert "PATRIMÔNIO TOTAL" in indicators.text
    future = client.get("/proximas-faturas?month=2026-08")
    assert future.status_code == 200
    assert "Próximas faturas" in future.text
    assert "R$ 100,00" in future.text
    simulator = client.get("/investimentos/simulador")
    assert simulator.status_code == 200
    assert "Simulador de investimentos" in simulator.text
    assert "IR regressivo" in simulator.text


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
