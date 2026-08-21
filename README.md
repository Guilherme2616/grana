# Grana — controle financeiro pessoal

Aplicação Flask independente para organizar contas, movimentações, categorias, cartões e importar faturas em PDF. O projeto foi preparado para uso pessoal e para a hospedagem gratuita do PythonAnywhere.

## O que já funciona

- Login privado com senha criptografada;
- Dashboard com saldo, entradas, saídas e últimas movimentações;
- Cadastro de contas, categorias e cartões;
- Edição das configurações padrão do cartão;
- Fechamento e vencimento específicos para cada mês/fatura;
- Lançamentos de receitas e despesas;
- Exclusão de lançamentos;
- Importação de fatura em PDF;
- Leitura de PDFs protegidos por senha, sem armazenar a senha;
- Sugestão automática do vencimento encontrado no PDF;
- Revisão de cada compra antes da confirmação;
- Conversão das compras confirmadas em lançamentos;
- Banco SQLite, adequado para um único usuário;
- Layout responsivo para computador e celular;
- Proteção CSRF e limite de 10 MB por PDF.
- Módulo de investimentos com compras, vendas, recebimentos e filtros;
- Cálculos de valor investido e saldo disponível na corretora.

> A leitura usa o texto interno do PDF. Como cada banco monta a fatura de uma forma, a tela de revisão é obrigatória. PDFs digitalizados como imagem ou com tabelas muito incomuns podem exigir um adaptador específico posteriormente.

PDFs protegidos por senha são aceitos. A senha existe apenas durante a requisição e não é gravada no banco, arquivo ou sessão. PDFs que sejam somente imagens ainda precisam de OCR.

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

Depois volte à aba **Web** e clique em **Reload**. O `upgrade.py` cria somente as novas tabelas e categorias que estiverem faltando, preservando seus registros atuais.

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

- Ajustar o leitor ao formato exato das suas faturas reais;
- Parcelas e recorrências;
- Edição de lançamentos e cadastros;
- Metas e planejamento mensal;
- Gráficos com dados reais;
- Exportação e backup pelo próprio sistema.
