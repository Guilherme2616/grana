import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pypdf import PdfReader, PdfWriter


DATE_PATTERN = re.compile(r"(?P<date>\d{2}/\d{2}(?:/\d{2,4})?)")
AMOUNT_TEXT = r"\d{1,3}(?:\.\d{3})*,\d{2}"
AMOUNT_PATTERN = re.compile(rf"(?P<amount>-?\s*(?:R\$\s*)?{AMOUNT_TEXT}-?)\s*$")
EXPIRY_WORD_PATTERN = re.compile(r"\b(?:data\s+de\s+)?vencimento\b|\bvence\b", re.IGNORECASE)
BB_SECTION_PATTERN = re.compile(
    r"(?:lan[cç]amentos|compras)(?:\s+e\s+compras|\s+realizados)?(?:\s+do\s+cart[aã]o)?"
    r"\s+(?:(?:n|d)esta|da|na)\s+fatura",
    re.IGNORECASE,
)
BB_TABLE_HEADER_PATTERN = re.compile(
    r"data\s+descri[cç][aã]o(?:\s+pa[ií]s)?\s+valor",
    re.IGNORECASE,
)
BB_TOTAL_LINE_PATTERN = re.compile(r"^total(?:\s|$)", re.IGNORECASE)
BB_END_PATTERN = re.compile(
    r"^(?:total(?:\s+(?:da|desta|sua))?\s+fatura|total\s+r\$|"
    r"parcelamentos?|compras?\s+parceladas?).*(?:pr[oó]xima|futura)?",
    re.IGNORECASE,
)
BB_COUNTRY_PATTERN = re.compile(r"\s+(?:[A-Z]{2}|\d{2})\s*$", re.IGNORECASE)
BB_ANY_AMOUNT_PATTERN = re.compile(rf"(?P<amount>-?\s*(?:R\$\s*)?{AMOUNT_TEXT}-?)", re.IGNORECASE)
MP_CARD_SECTION_PATTERN = re.compile(r"cart[aã]o\s+(?:visa|mastercard|elo|american\s+express)", re.IGNORECASE)
INSTALLMENT_PATTERN = re.compile(r"\bparcela\s+(?P<current>\d+)\s+de\s+(?P<total>\d+)\b", re.IGNORECASE)
SLASH_INSTALLMENT_PATTERN = re.compile(
    r"\b(?:parc(?:ela)?[-\s]*)?(?P<current>\d{1,2})\s*/\s*(?P<total>\d{1,2})\b",
    re.IGNORECASE,
)
INTER_SECTION_PATTERN = re.compile(r"despesas\s+da\s+fatura", re.IGNORECASE)
INTER_TOTAL_PATTERN = re.compile(r"^total\s+cart[aã]o\b", re.IGNORECASE)
INTER_AMOUNT_PATTERN = re.compile(rf"(?P<amount>R\$\s*{AMOUNT_TEXT})\s*$", re.IGNORECASE)
INTER_DATE_PATTERN = re.compile(
    r"^(?P<day>\d{1,2})\s+de\s+(?P<month>jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)\.?(?:\s+de)?\s+(?P<year>\d{4})(?:\s+|$)",
    re.IGNORECASE,
)
PT_MONTHS = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}
ITAU_SECTION_PATTERN = re.compile(
    r"lan[cç]amentos\s*(?::|-)?\s*(?:compras\s+e\s+saques|do\s+cart[aã]o)",
    re.IGNORECASE,
)
ITAU_END_PATTERN = re.compile(
    r"^(?:total\s+dos\s+lan[cç]amentos\s+atuais|compras\s+parceladas\s*-?\s*pr[oó]ximas\s+faturas)",
    re.IGNORECASE,
)
ITAU_FEES_SECTION_PATTERN = re.compile(r"encargos\s+cobrados\s+nesta\s+fatura", re.IGNORECASE)
ITAU_FEE_PATTERN = re.compile(
    rf"^(?P<description>juros\s+do\s+rotativo|juros\s+de\s+mora|multa\s+por\s+atraso|iof\s+de\s+financiamento)\b"
    rf".*?(?P<amount>{AMOUNT_TEXT})\s*$",
    re.IGNORECASE,
)
ITAU_DATE_TOKEN_PATTERN = re.compile(r"(?P<date>\d{2}/\d{2})(?=\s+)")


class PdfPasswordRequired(ValueError):
    pass


class PdfPasswordInvalid(ValueError):
    pass


def parse_brl(value):
    cleaned = value.replace("R$", "").replace(" ", "")
    negative = cleaned.startswith("-") or cleaned.endswith("-")
    cleaned = cleaned.strip("-").replace(".", "").replace(",", ".")
    try:
        result = Decimal(cleaned)
        return -result if negative else result
    except InvalidOperation as exc:
        raise ValueError(f"Valor inválido: {value}") from exc


def parse_date(value, reference_month):
    month_year = datetime.strptime(reference_month, "%Y-%m")
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    day, month = [int(part) for part in value.split("/")]
    year = month_year.year
    # A fatura nunca contém uma compra futura. Se o mês impresso é posterior
    # ao mês da fatura, a compra pertence ao ano anterior (ex.: 28/11 em uma
    # fatura com vencimento em agosto do ano seguinte).
    if month > month_year.month:
        year -= 1
    return date(year, month, day)


def detect_due_date(text, reference_month):
    lines = [" ".join(raw_line.split()) for raw_line in text.splitlines()]
    for index, line in enumerate(lines):
        if not EXPIRY_WORD_PATTERN.search(line):
            continue
        window = " ".join(lines[index:index + 3])
        date_match = DATE_PATTERN.search(window)
        if date_match:
            try:
                return parse_date(date_match.group("date"), reference_month)
            except ValueError:
                continue
    return None


def detect_bb_statement_total(text):
    section_match = BB_SECTION_PATTERN.search(text)
    cover = text[:section_match.start()] if section_match else text
    patterns = (
        re.compile(
            rf"\b(?:valor(?:\s+total)?|total(?:\s+(?:da|desta|sua)\s+fatura)?|total\s+a\s+pagar)\b"
            rf"\s*(?::|[eé]\s*:)?\s*(?:\r?\n|\s)+(?:R\$\s*)?(?P<amount>{AMOUNT_TEXT})",
            re.IGNORECASE,
        ),
        re.compile(rf"^\s*Total\s+(?:R\$\s*)?(?P<amount>{AMOUNT_TEXT})\s*$", re.IGNORECASE | re.MULTILINE),
    )
    for pattern in patterns:
        match = pattern.search(cover)
        if match:
            return parse_brl(match.group("amount"))
    return None


def is_bb_smiles_invoice(text):
    normalized = text.lower()
    compact = re.sub(r"\s+", "", normalized)
    return (
        (
            "banco do brasil" in normalized
            or "bancodobrasil" in compact
            or "bb.com.br" in normalized
            or "ourocard" in normalized
        )
        and (
            BB_SECTION_PATTERN.search(text) is not None
            or BB_TABLE_HEADER_PATTERN.search(text) is not None
        )
    )


def is_mercado_pago_invoice(text):
    normalized = text.lower()
    return "mercado pago" in normalized and "detalhes de consumo" in normalized


def is_banco_inter_invoice(text):
    normalized = text.lower()
    return (
        re.search(r"\binter\b", normalized) is not None
        and "resumo da fatura" in normalized
        and INTER_SECTION_PATTERN.search(text) is not None
    )


def is_itau_invoice(text):
    normalized = text.lower()
    return (
        ("itaú" in normalized or re.search(r"\bitau\b", normalized) is not None)
        and ITAU_SECTION_PATTERN.search(text) is not None
        and "limite total de crédito" in normalized
    )


def _find_labeled_amount(text, label):
    pattern = re.compile(
        rf"{label}\s*(?::|[eé]\s*:)?\s*(?:\r?\n|\s)+R\$\s*(?P<amount>{AMOUNT_TEXT})",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    return parse_brl(match.group("amount")) if match else None


def parse_mercado_pago_summary(first_page_text, reference_month):
    due_date = detect_due_date(first_page_text, reference_month)
    total = _find_labeled_amount(first_page_text, r"total\s+a\s+pagar")
    credit_limit = _find_labeled_amount(first_page_text, r"limite\s+total")
    cash_advance_total = _find_labeled_amount(first_page_text, r"saque\s+total")

    # No modo layout, os quatro títulos podem vir em uma linha e os valores na seguinte.
    lines = [" ".join(line.split()) for line in first_page_text.splitlines()]
    for index, line in enumerate(lines):
        lowered = line.lower()
        if "total a pagar" not in lowered or "limite total" not in lowered or "saque total" not in lowered:
            continue
        window = " ".join(lines[index:index + 4])
        amounts = re.findall(rf"R\$\s*({AMOUNT_TEXT})", window)
        if len(amounts) >= 3:
            # Na extração em modo layout, os títulos ficam na mesma linha e os
            # valores preservam a mesma ordem visual logo abaixo deles.
            total = parse_brl(amounts[0])
            credit_limit = parse_brl(amounts[1])
            cash_advance_total = parse_brl(amounts[2])
        break

    return {
        "due_date": due_date,
        "statement_total": total,
        "credit_limit": credit_limit,
        "cash_advance_total": cash_advance_total,
    }


def parse_banco_inter_summary(first_page_text, second_page_text, reference_month):
    """Lê os dados gerais; o total contábil é 'Despesas do mês'."""
    return {
        "due_date": detect_due_date(first_page_text, reference_month),
        "statement_total": _find_labeled_amount(second_page_text, r"despesas\s+do\s+m[eê]s"),
        "credit_limit": _find_labeled_amount(first_page_text, r"limite\s+de\s+cr[eé]dito\s+total"),
        "cash_advance_total": None,
    }


def _find_itau_total(text, label):
    pattern = re.compile(
        rf"^\s*{label}\s+(?:R\$\s*)?(?P<amount>{AMOUNT_TEXT})\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(text)
    return parse_brl(match.group("amount")) if match else None


def parse_itau_summary(first_page_text, third_page_text, reference_month):
    cover_total = _find_labeled_amount(
        first_page_text,
        r"(?:o\s+)?total\s+da\s+sua\s+fatura",
    )
    components = (
        _find_itau_total(third_page_text, r"lan[cç]amentos\s+no\s+cart[aã]o"),
        _find_itau_total(third_page_text, r"lan[cç]amentos\s+produtos\s+e\s+servi[cç]os"),
        _find_itau_total(third_page_text, r"total\s+de\s+encargos\s+em\s+R\$"),
    )
    calculated_total = sum(components, Decimal("0")) if all(value is not None for value in components) else None
    return {
        "due_date": detect_due_date(first_page_text, reference_month),
        "statement_total": calculated_total or cover_total,
        "cover_total": cover_total,
        "components": components,
        "credit_limit": _find_labeled_amount(            first_page_text,
            r"limite\s+total\s+de\s+cr[eé]dito",
        ),
        "cash_advance_total": None,
    }


def _append_item(
    items,
    seen,
    raw_date,
    raw_description,
    raw_amount,
    reference_month,
    installment_current=None,
    installment_total=None,
):
    description = " ".join(raw_description.split()).strip(" -–—·|")
    description = BB_COUNTRY_PATTERN.sub("", description).strip()
    if len(description) < 2:
        return
    try:
        purchase_date = parse_date(raw_date, reference_month)
        amount = parse_brl(raw_amount)
    except (ValueError, InvalidOperation):
        return

    # Pagamentos e créditos aparecem com sinal negativo no BB e não são compras.
    if amount <= 0:
        return
    identity = (purchase_date.isoformat(), description.lower(), str(amount))
    if identity in seen:
        return
    seen.add(identity)
    items.append({
        "purchase_date": purchase_date,
        "description": description[:180],
        "amount": amount,
        "installment_current": installment_current,
        "installment_total": installment_total,
    })


def parse_bb_smiles_text(text, reference_month):
    section_match = BB_SECTION_PATTERN.search(text)
    if not section_match:
        section_match = BB_TABLE_HEADER_PATTERN.search(text)
    if section_match:
        section = text[section_match.end():]
    else:
        # Alguns PDFs do BB desenham o título e o cabeçalho como imagem, mas
        # mantêm as linhas da tabela como texto. Nesse caso, começa na primeira
        # linha datada; pagamentos negativos continuam sendo descartados.
        first_row = re.search(r"(?m)^\s*\d{2}/\d{2}(?:/\d{2,4})?\s+", text)
        if not first_row:
            return []
        section = text[first_row.start():]
    items = []
    seen = set()
    pending_date = None
    pending_parts = []

    for raw_line in section.splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue
        lowered = line.lower()
        if BB_END_PATTERN.match(line) and not lowered.startswith("totalpass"):
            break
        if (
            BB_TABLE_HEADER_PATTERN.search(line)
            or BB_SECTION_PATTERN.search(line)
            or lowered in {"pagamentos", "compras", "compras diversas", "lançamentos", "data", "descrição", "país", "valor"}
        ):
            continue

        date_match = re.match(r"^(?P<date>\d{2}/\d{2}(?:/\d{2,4})?)(?:\s+|$)(?P<rest>.*)$", line)
        if date_match:
            pending_date = date_match.group("date")
            pending_parts = [date_match.group("rest")] if date_match.group("rest") else []
        elif pending_date:
            pending_parts.append(line)
        else:
            continue

        combined = " ".join(pending_parts)
        # Algumas versões extraem a coluna País depois do valor; por isso o
        # leitor do BB não exige mais que o valor seja o último texto da linha.
        # Em compras internacionais o PDF pode trazer o valor na moeda
        # original e, por último, o valor efetivamente lançado em reais.
        amount_matches = list(BB_ANY_AMOUNT_PATTERN.finditer(combined))
        if amount_matches:
            amount_match = amount_matches[-1]
            description = combined[:amount_match.start()]
            normalized_description = re.sub(r"[^a-z0-9]+", " ", description.lower()).strip()
            # Em PDFs digitalizados o sinal negativo do pagamento pode não ser
            # reconhecido pelo OCR. A descrição ainda permite descartá-lo com
            # segurança antes de criar uma compra falsa.
            if normalized_description.startswith(("pgto ", "pagamento ", "pag fatura ")):
                pending_date = None
                pending_parts = []
                continue
            installment_match = SLASH_INSTALLMENT_PATTERN.search(description)
            _append_item(
                items,
                seen,
                pending_date,
                description,
                amount_match.group("amount"),
                reference_month,
                int(installment_match.group("current")) if installment_match else None,
                int(installment_match.group("total")) if installment_match else None,
            )
            pending_date = None
            pending_parts = []

    return items


def parse_mercado_pago_text(text, reference_month):
    section_match = MP_CARD_SECTION_PATTERN.search(text)
    if not section_match:
        return []

    section = text[section_match.end():]
    items = []
    seen = set()
    pending_date = None
    pending_parts = []

    for raw_line in section.splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue
        if BB_TOTAL_LINE_PATTERN.match(line) and not line.lower().startswith("totalpass"):
            break

        date_match = re.match(r"^(?P<date>\d{2}/\d{2}(?:/\d{2,4})?)(?:\s+|$)(?P<rest>.*)$", line)
        if date_match:
            pending_date = date_match.group("date")
            pending_parts = [date_match.group("rest")] if date_match.group("rest") else []
        elif pending_date:
            pending_parts.append(line)
        else:
            continue

        combined = " ".join(pending_parts)
        amount_match = AMOUNT_PATTERN.search(combined)
        if not amount_match:
            continue

        description_and_installment = combined[:amount_match.start()].strip()
        installment_match = INSTALLMENT_PATTERN.search(description_and_installment)
        installment_current = None
        installment_total = None
        if installment_match:
            installment_current = int(installment_match.group("current"))
            installment_total = int(installment_match.group("total"))
            description = INSTALLMENT_PATTERN.sub("", description_and_installment).strip()
        else:
            description = description_and_installment

        _append_item(
            items,
            seen,
            pending_date,
            description,
            amount_match.group("amount"),
            reference_month,
            installment_current,
            installment_total,
        )
        pending_date = None
        pending_parts = []

    return items


def parse_banco_inter_text(text, reference_month):
    section_match = INTER_SECTION_PATTERN.search(text)
    if not section_match:
        return []

    section = text[section_match.end():]
    items = []
    seen = set()
    pending_date = None
    pending_parts = []

    for raw_line in section.splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue
        if INTER_TOTAL_PATTERN.match(line):
            break

        date_match = INTER_DATE_PATTERN.match(line)
        if date_match:
            purchase_date = date(
                int(date_match.group("year")),
                PT_MONTHS[date_match.group("month").lower()],
                int(date_match.group("day")),
            )
            pending_date = purchase_date.strftime("%d/%m/%Y")
            rest = line[date_match.end():].strip()
            pending_parts = [rest] if rest else []
        elif pending_date:
            pending_parts.append(line)
        else:
            continue

        combined = " ".join(pending_parts)
        # Créditos/pagamentos do Inter aparecem com um '+' antes do valor.
        if re.search(rf"\+\s*R\$\s*{AMOUNT_TEXT}\s*$", combined):
            pending_date = None
            pending_parts = []
            continue
        amount_match = INTER_AMOUNT_PATTERN.search(combined)
        if not amount_match:
            continue

        description_and_installment = combined[:amount_match.start()].strip()
        installment_match = INSTALLMENT_PATTERN.search(description_and_installment)
        installment_current = None
        installment_total = None
        if installment_match:
            installment_current = int(installment_match.group("current"))
            installment_total = int(installment_match.group("total"))
            description = INSTALLMENT_PATTERN.sub("", description_and_installment)
            description = re.sub(r"\(\s*\)", "", description).strip()
        else:
            description = description_and_installment

        _append_item(
            items,
            seen,
            pending_date,
            description,
            amount_match.group("amount"),
            reference_month,
            installment_current,
            installment_total,
        )
        pending_date = None
        pending_parts = []

    return items


def parse_itau_text(text, reference_month):    section_match = ITAU_SECTION_PATTERN.search(text)
    if not section_match:
        return []

    section = text[section_match.end():]
    items = []
    column_items = [[], []]
    seen = set()
    pending_date = None
    pending_parts = []
    pending_column = 0

    def append_segment(row_date, remainder, column=0):
        # No PDF real do Itaú, a compra da coluna esquerda termina no valor,
        # mas a mesma linha continua com categoria/cidade. Por isso o valor não
        # pode ser obrigado a estar no fim da linha. A primeira quantia depois
        # da descrição é o valor da compra; tokens como 09/10 não têm vírgula e
        # não são confundidos com dinheiro.
        amount_match = BB_ANY_AMOUNT_PATTERN.search(remainder)
        if not amount_match:
            return False
        description = remainder[:amount_match.start()].strip()
        installment_match = SLASH_INSTALLMENT_PATTERN.search(description) or INSTALLMENT_PATTERN.search(description)
        previous_count = len(items)
        _append_item(
            items,
            seen,
            row_date,
            description,
            amount_match.group("amount"),
            reference_month,
            int(installment_match.group("current")) if installment_match else None,
            int(installment_match.group("total")) if installment_match else None,
        )
        if len(items) > previous_count:
            column_items[column].append(items.pop())
        return True

    def row_date_matches(raw_line):
        matches = []
        for match in ITAU_DATE_TOKEN_PATTERN.finditer(raw_line):
            prefix = raw_line[:match.start()]
            if not prefix.strip():
                matches.append(match)
                continue
            if not re.search(r"\s{2,}$", prefix):
                continue
            trailing = raw_line[match.end():].lstrip()
            # Em PDFs com colunas bem espaçadas, 09/10 de uma parcela pode
            # parecer uma segunda data. Uma data de linha é seguida pelo nome
            # do estabelecimento; a parcela é seguida diretamente pelo valor.
            if re.match(rf"(?:R\$\s*)?{AMOUNT_TEXT}(?:\s|$)", trailing, re.IGNORECASE):
                continue
            matches.append(match)
        return matches

    for raw_line in section.splitlines():
        normalized_line = " ".join(raw_line.split()).strip()
        if not normalized_line:
            continue
        if ITAU_END_PATTERN.match(normalized_line):
            break

        # A página 2 possui duas tabelas lado a lado. Os espaços preservados pelo
        # modo layout permitem separar duas compras que estejam na mesma linha.
        date_matches = row_date_matches(raw_line)
        if not date_matches:
            if pending_date:
                pending_parts.append(normalized_line)
                if append_segment(pending_date, " ".join(pending_parts), pending_column):
                    pending_date = None
                    pending_parts = []
            continue

        pending_date = None
        pending_parts = []
        for index, date_match in enumerate(date_matches):
            start = date_match.start("date")
            end = date_matches[index + 1].start("date") if index + 1 < len(date_matches) else len(raw_line)
            segment = " ".join(raw_line[start:end].split()).strip()
            row_date = date_match.group("date")
            remainder = segment[len(row_date):].strip()
            # A coluna direita começa bem depois da margem da esquerda. Não
            # removemos a indentação original porque ela é a única pista em
            # linhas que contêm apenas uma compra da coluna direita.
            column = 0 if date_match.start() < 100 else 1
            if not append_segment(row_date, remainder, column) and len(date_matches) == 1:
                pending_date = row_date
                pending_parts = [remainder]
                pending_column = column

    # O Itaú imprime duas colunas independentes. A revisão deve seguir a ordem
    # visual da fatura: de cima a baixo na esquerda e, depois, na direita.
    items.extend(column_items[0])
    items.extend(column_items[1])

    fees_match = ITAU_FEES_SECTION_PATTERN.search(text)
    if fees_match:
        reference = datetime.strptime(reference_month, "%Y-%m").date()
        for raw_line in text[fees_match.end():].splitlines():
            line = " ".join(raw_line.split()).strip()
            if re.match(r"^total\s+de\s+encargos", line, re.IGNORECASE):
                break
            fee_match = ITAU_FEE_PATTERN.match(line)
            if not fee_match:
                continue
            amount = parse_brl(fee_match.group("amount"))
            description = "Encargo Itaú — " + fee_match.group("description").strip().title()
            identity = (reference.isoformat(), description.lower(), str(amount))
            if amount <= 0 or identity in seen:
                continue
            seen.add(identity)
            items.append({
                "purchase_date": reference,
                "description": description,
                "amount": amount,
                "installment_current": None,
                "installment_total": None,
            })

    return items


def parse_generic_text(text, reference_month):
    items = []
    seen = set()
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        date_match = DATE_PATTERN.search(line)
        amount_match = AMOUNT_PATTERN.search(line)
        if not date_match or not amount_match or date_match.start() > amount_match.start():
            continue
        description = line[date_match.end():amount_match.start()]
        _append_item(
            items,
            seen,
            date_match.group("date"),
            description,
            amount_match.group("amount"),
            reference_month,
        )
    return items


def extract_pdf_pages(reader):
    pages = []
    for page in reader.pages:
        try:
            page_text = page.extract_text(extraction_mode="layout") or ""
        except (TypeError, ValueError):
            page_text = page.extract_text() or ""
        pages.append(page_text)
    return pages


def _bb_compact_table_image(image):
    """Recorta somente as linhas datadas da grade do BB digitalizado.

    A fatura Smiles é uma imagem de página inteira. Reconhecer publicidade,
    textos legais e caixas de resumo torna o Tesseract inviável no servidor
    de produção. As linhas horizontais da tabela, por outro lado, são estáveis
    e permitem montar uma imagem pequena somente com os lançamentos.
    """
    from PIL import Image, ImageOps

    prepared = ImageOps.autocontrast(image.convert("L"))
    width, height = prepared.size
    left = int(width * 0.05)
    right = int(width * 0.95)

    # Soma horizontal feita pelo Pillow (em C), evitando percorrer todos os
    # pixels em Python no ambiente de CPU limitada do PythonAnywhere.
    dark = prepared.point(lambda value: 255 if value < 230 else 0)
    row_density = dark.crop((left, 0, right, height)).resize(
        (1, height),
        Image.Resampling.BOX,
    )
    horizontal = [index for index, value in enumerate(row_density.getdata()) if value > 165]

    groups = []
    for y in horizontal:
        if not groups or y > groups[-1][-1] + 1:
            groups.append([y])
        else:
            groups[-1].append(y)
    lines = [sum(group) // len(group) for group in groups]

    minimum_gap = max(12, int(height * 0.012))
    maximum_gap = max(30, int(height * 0.03))
    runs = []
    current = []
    for y in lines:
        if current and not minimum_gap <= y - current[-1] <= maximum_gap:
            if len(current) >= 5:
                runs.append(current)
            current = []
        current.append(y)
    if len(current) >= 5:
        runs.append(current)
    if not runs:
        return None

    table_lines = max(runs, key=len)
    candidates = []
    first_column_left = int(width * 0.065)
    first_column_right = int(width * 0.13)
    content_left = int(width * 0.06)
    content_right = int(width * 0.94)
    for row_index, (top, bottom) in enumerate(zip(table_lines, table_lines[1:])):
        if bottom - top < 8:
            continue
        date_cell = prepared.crop((first_column_left, top + 2, first_column_right, bottom - 1))
        dark_pixels = sum(1 for value in date_cell.getdata() if value < 160)
        if dark_pixels > 40:
            candidates.append((row_index, prepared.crop((content_left, top + 2, content_right, bottom - 1))))

    if not candidates:
        return None

    # Na página final, a grade repete lançamentos futuros depois de um bloco de
    # subtotal/total. Essa separação produz várias linhas sem data; ao detectar
    # o salto, conserva somente o primeiro bloco (a fatura atual).
    cutoff = len(candidates)
    for index in range(1, len(candidates)):
        if candidates[index][0] - candidates[index - 1][0] >= 4:
            cutoff = index
            break
    rows = [row for _, row in candidates[:cutoff]]
    if not rows:
        return None

    spacing = 4
    compact = Image.new(
        "L",
        (max(row.width for row in rows), sum(row.height for row in rows) + spacing * (len(rows) - 1)),
        255,
    )
    y = 0
    for row in rows:
        compact.paste(row, (0, y))
        y += row.height + spacing
    return compact


def _normalize_bb_ocr_text(recognized):
    """Corrige confusões previsíveis do Tesseract nas linhas do BB."""
    # Limita a correção ao campo de data no início da linha. Assim, nomes de
    # estabelecimentos e números de parcelas permanecem exatamente como o OCR
    # os reconheceu. No PDF real, 18/08 foi lido como 1g/o8.
    def normalize_date(match):
        raw_date = match.group("date")
        normalized_date = raw_date.translate(str.maketrans({
            "g": "8", "G": "8", "o": "0", "O": "0",
        }))
        return f"{match.group('prefix')}{normalized_date}"

    recognized = re.sub(
        r"(?m)^(?P<prefix>\\s*)(?P<date>[0-9gGoO]{2}/[0-9gGoO]{2})(?=\\s|$)",
        normalize_date,
        recognized,
    )
    recognized = re.sub(r"\\bR[SG§g]\\s*(?=\\d)", "R$ ", recognized, flags=re.IGNORECASE)
    return re.sub(r"(?<=\\d)\\.(?=\\d{2}(?:\\D|$))", ",", recognized)


def ocr_pdf_pages(reader):
    """Renderiza e reconhece PDFs digitalizados que não possuem camada de texto."""
    if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        return []
    try:
        from PIL import Image
        import pytesseract
    except ImportError:
        return []

    with tempfile.TemporaryDirectory(prefix="grana-invoice-") as directory:
        temporary = Path(directory)
        decrypted_pdf = temporary / "invoice.pdf"
        writer = PdfWriter()
        writer.clone_document_from_reader(reader)
        with decrypted_pdf.open("wb") as output:
            writer.write(output)

        prefix = temporary / "page"
        try:
            subprocess.run(
                # A primeira página contém apenas o resumo. Os lançamentos do
                # layout BB Smiles começam na página 2.
                ["pdftoppm", "-f", "2", "-png", "-gray", "-r", "130", str(decrypted_pdf), str(prefix)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=90,
            )
        except (OSError, subprocess.SubprocessError):
            return []

        pages = []
        for image_path in sorted(temporary.glob("page-*.png")):
            try:
                with Image.open(image_path) as image:
                    prepared = _bb_compact_table_image(image)
                    if prepared is None:
                        pages.append("")
                        continue
                    recognized = pytesseract.image_to_string(
                        prepared,
                        config="--psm 6",
                        timeout=120,
                    )
                    # Normaliza duas confusões recorrentes sem alterar nomes:
                    # símbolo monetário (RS/R§) e separador decimal por ponto.
                    pages.append(_normalize_bb_ocr_text(recognized))
            except (OSError, RuntimeError):
                pages.append("")
        return pages


def parse_invoice_pdf(stream, reference_month, password="", expected_provider=""):
    reader = PdfReader(stream)
    if reader.is_encrypted:
        if not password:
            raise PdfPasswordRequired("Este PDF exige uma senha.")
        if not reader.decrypt(password):
            raise PdfPasswordInvalid("A senha informada não desbloqueou o PDF.")

    pages = extract_pdf_pages(reader)
    # A fatura BB Smiles enviada pelo usuário é inteiramente digitalizada: o
    # pypdf abre o documento, mas todas as páginas retornam texto vazio. Nesse
    # caso, renderiza a página completa e aplica OCR antes de escolher o leitor.
    if expected_provider == "bb_smiles" and not any(page.strip() for page in pages):
        pages = ocr_pdf_pages(reader)
    text = "\n".join(pages)
    due_date = detect_due_date(text, reference_month)
    resolved_reference = due_date.strftime("%Y-%m") if due_date else reference_month

    if expected_provider == "bb_smiles" or is_bb_smiles_invoice(text):
        items = parse_bb_smiles_text(text, resolved_reference)
        statement_total = detect_bb_statement_total(text)
        adapter = "bb_smiles"
        credit_limit = None
        cash_advance_total = None
    elif expected_provider == "mercado_pago" or is_mercado_pago_invoice(text):
        summary = parse_mercado_pago_summary(pages[0] if pages else text, reference_month)
        due_date = summary["due_date"] or due_date
        resolved_reference = due_date.strftime("%Y-%m") if due_date else reference_month
        details_text = "\n".join(pages[1:]) if len(pages) > 1 else text
        items = parse_mercado_pago_text(details_text, resolved_reference)
        statement_total = summary["statement_total"]
        credit_limit = summary["credit_limit"]
        cash_advance_total = summary["cash_advance_total"]
        adapter = "mercado_pago"
    elif expected_provider == "banco_inter" or is_banco_inter_invoice(text):
        first_page = pages[0] if pages else text
        second_page = pages[1] if len(pages) > 1 else text
        summary = parse_banco_inter_summary(first_page, second_page, reference_month)
        due_date = summary["due_date"] or due_date
        resolved_reference = due_date.strftime("%Y-%m") if due_date else reference_month
        # Neste modelo, somente a página 3 contém as despesas analíticas atuais.
        details_text = pages[2] if len(pages) > 2 else text
        items = parse_banco_inter_text(details_text, resolved_reference)
        statement_total = summary["statement_total"]
        credit_limit = summary["credit_limit"]
        cash_advance_total = None
        adapter = "banco_inter"
    elif expected_provider == "itau" or is_itau_invoice(text):
        first_page = pages[0] if pages else text
        third_page = pages[2] if len(pages) > 2 else text
        summary = parse_itau_summary(first_page, third_page, reference_month)
        due_date = summary["due_date"] or due_date
        resolved_reference = due_date.strftime("%Y-%m") if due_date else reference_month
        # Faturas longas podem espalhar os lançamentos por quatro ou mais
        # páginas. O próprio parser interrompe no total dos lançamentos atuais,
        # antes da seção de parcelas futuras.
        details_text = "\n".join(pages[1:]) if len(pages) > 1 else text
        items = parse_itau_text(details_text, resolved_reference)
        statement_total = summary["statement_total"]
        credit_limit = summary["credit_limit"]
        cash_advance_total = None
        adapter = "itau"
    else:
        items = parse_generic_text(text, resolved_reference)
        statement_total = None
        adapter = "generic"
        credit_limit = None
        cash_advance_total = None

    return {
        "items": items,
        "due_date": due_date,
        "statement_total": statement_total,
        "reference_month": resolved_reference,
        "adapter": adapter,
        "credit_limit": credit_limit,
        "cash_advance_total": cash_advance_total,
    }