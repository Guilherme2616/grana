# Grana — controle financeiro pessoal

Aplicação Flask independente para organizar contas, movimentações, categorias, cartões e importar faturas em PDF. O projeto foi preparado para uso pessoal e para a hospedagem gratuita do PythonAnywhere.

## O que já funciona

- Login privado com senha criptografada;
- Dashboard com saldo, entradas, saídas e últimas movimentações;
- Central de indicadores com filtros mensais, patrimônio, evolução, categorias, cartões, origem das compras e alertas;
- Cadastro de contas, categorias e cartões;
- Edição das configurações padrão do cartão;
- Fechamento e vencimento específicos para cada mês/fatura;
- Lançamentos de receitas e despesas;
- Exclusão de lançamentos;
- Importação de fatura em PDF;
- Sincronização mensal em lote com uma pasta do Google Drive;
- Leitores específicos para Banco do Brasil Smiles, Mercado Pago, Banco Inter e Itaú;
- Leitura de PDFs protegidos por senha e armazenamento opcional criptografado por cartão;
- Sugestão automática do vencimento encontrado no PDF;
- Revisão de cada compra antes da confirmação;
- Central de faturas com filtros para pendentes e confirmadas;
- Continuação de uma revisão mesmo depois de sair da tela ou encerrar a sessão;
- Descarte seguro de rascunhos para permitir uma nova importação;
- Reprocessamento de PDFs do Google Drive sem criar duplicações;
- Conversão das compras confirmadas em lançamentos;
- Banco SQLite, adequado para um único usuário;
- Layout responsivo para computador e celular;
- Proteção CSRF e limite de 10 MB por PDF.
- Módulo de investimentos com compras, vendas, recebimentos e filtros;
- Cálculos de valor investido e saldo disponível na corretora.
- Provisionamento de parcelas para as próximas 12 faturas, substituído pelo valor real quando a fatura já estiver confirmada;
- Simulador de investimentos e poupança com aportes, cenários, inflação, IR regressivo, resultado líquido e evolução anual.

> A leitura usa o texto interno do PDF. No Banco do Brasil Smiles, aceita variações do título da tabela, descrições quebradas em várias linhas e a coluna País antes ou depois do valor; considera somente os lançamentos atuais até “Total”. No Mercado Pago, começa na tabela do cartão, ignora pagamentos de faturas, identifica parcelas e termina em “Total”, mesmo quando a tabela ocupa mais páginas. No Banco Inter, usa “Despesas do mês” como total e importa somente os gastos da página analítica, descartando pagamentos/créditos e as páginas posteriores. No Itaú, começa em “Lançamentos: compras e saques”, inclui produtos e serviços, encerra antes das próximas faturas e confere os subtotais com o total da capa. A tela de revisão continua obrigatória.

PDFs protegidos por senha são aceitos. Na importação manual, a senha é descartada depois da leitura. Para a sincronização do Drive, ela pode ser salva criptografada na configuração do cartão. A chave secreta do aplicativo precisa permanecer estável para que o sistema consiga descriptografá-la. PDFs que sejam somente imagens ainda precisam de OCR.

## Sincronizar faturas pelo Google Drive

Estrutura esperada dentro da pasta principal compartilhada:

```text
Faturas Grana/
└── 2026/
    └── AGOSTO/
        ├── Banco do Brasil - Smiles.pdf
        ├── Mercado Pago.pdf
        ├── Banco Inter.pdf
        └── Itaú.pdf
```

Também são aceitos nomes de mês como `08 - AGOSTO`. O aplicativo acessa a pasta somente para leitura e registra o ID de cada arquivo importado para impedir duplicações.

### Configuração

1. Crie um projeto no Google Cloud e habilite a **Google Drive API**;
2. Crie uma **conta de serviço** e gere uma chave JSON;
3. No Google Drive, compartilhe somente a pasta `Faturas Grana` como leitor com o e-mail da conta de serviço;
4. No PythonAnywhere, crie `~/grana/instance` e envie a chave como `google-service-account.json`;
5. Copie o ID da pasta `Faturas Grana` a partir do endereço exibido pelo Drive;
6. Adicione ao arquivo WSGI, antes de importar a aplicação:

```python
os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"] = "/home/SEU_USUARIO/grana/instance/google-service-account.json"
os.environ["GOOGLE_DRIVE_ROOT_FOLDER_ID"] = "ID_DA_PASTA_FATURAS_GRANA"
```

7. Recarregue o Web App;
8. No Grana, abra cada cartão, escolha seu banco e salve a senha do PDF, caso exista;
9. Em **Importar faturas**, selecione o mês e clique em **Sincronizar pasta**.

As faturas são criadas como rascunho. Cada uma deve ser revisada e confirmada antes de virar lançamento. Se você sair da tela, abra **Faturas → Aguardando revisão** para continuar. Um rascunho do Drive pode ser reprocessado; ao descartá-lo, o mesmo PDF fica liberado para uma nova importação.

## Rodar no computador

Use Python 3.10 ou superior.

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux ou macOS
source venv/bin/activate

pip install -r requirements.txt
python setup_admin.py
python run.py
```

Abra `http://127.0.0.1:5000`.

## Publicar gratuitamente no PythonAnywhere

### 1. Enviar os arquivos

No painel **Files**, envie o ZIP e extraia-o em:

```text
/home/SEU_USUARIO/novo_financeiro
```

Também é possível enviar o projeto pelo GitHub e cloná-lo no console Bash.

### 2. Criar o ambiente virtual

Abra um console Bash:

```bash
cd ~/novo_financeiro
python3.10 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Criar seu usuário

Ainda no console:

```bash
python setup_admin.py
```

Escolha seu nome de usuário e uma senha com ao menos oito caracteres. A senha será salva somente como hash.

### 4. Criar o Web App

No painel **Web**:

1. Clique em **Add a new web app**;
2. Escolha **Manual configuration**;
3. Selecione Python 3.10;
4. Em **Virtualenv**, informe `/home/SEU_USUARIO/novo_financeiro/venv`;
5. Abra o arquivo de configuração WSGI.

Substitua o conteúdo do WSGI por:

```python
import os
import sys

project_home = "/home/SEU_USUARIO/novo_financeiro"
if project_home not in sys.path:
    sys.path.insert(0, project_home)

os.environ["SECRET_KEY"] = "COLOQUE-AQUI-UMA-CHAVE-LONGA-E-ALEATORIA"

from wsgi import application
```

Troque `SEU_USUARIO` e a chave secreta. Para gerar uma chave no console:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Clique em **Reload**. O sistema estará disponível no endereço mostrado pelo PythonAnywhere.

## Atualizar uma instalação existente

Depois de enviar uma nova versão ao GitHub, execute no console do PythonAnywhere:

```bash
cd ~/grana
git pull origin main
source venv/bin/activate
pip install -r requirements.txt
python upgrade.py
```

Depois volte à aba **Web** e clique em **Reload**. O `upgrade.py` cria somente as novas tabelas, colunas e categorias que estiverem faltando, preservando seus registros atuais. Nesta versão, ele também prepara os campos persistentes usados pela central de faturas.

## Backup

Todos os dados ficam em `financeiro.db`. Para fazer backup, baixe esse arquivo com o site parado ou sem lançamentos sendo gravados naquele momento. Não publique esse arquivo no GitHub.

## Estrutura principal

```text
novo_financeiro/
├── app/
│   ├── services/invoice_parser.py
│   ├── static/css/style.css
│   ├── templates/
│   ├── models.py
│   └── routes.py
├── tests/
├── config.py
├── requirements.txt
├── run.py
├── setup_admin.py
└── wsgi.py
```

## Próximas evoluções sugeridas

- Adicionar leitores específicos para outros bancos;
- Recorrências;
- Edição de lançamentos e cadastros;
- Metas e planejamento mensal;
- Gráficos com dados reais;
- Exportação e backup pelo próprio sistema.
