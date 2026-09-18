import hashlib
import re
import shutil
import subprocess
import tempfile
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

DATE_HEADING = re.compile(
    r"(?m)^\s*(?P<day>\d{1,2})\s+de\s+(?P<month>[A-Za-zÀ-ÿ]+)\s+de\s+(?P<year>\d{4})\s*$",
    re.IGNORECASE,
)
MOVEMENT_START = re.compile(
    r"(?m)^\s{2,}(?P<kind>Juros\s+Sobre\s+Capital|Dividendo|Rendimento)\b",
    re.IGNORECASE,
)
TICKER = re.compile(r"\b(?P<ticker>[A-Z]{4}\d{1,2})\s*-\s*(?P<name>.+?)"
                    r"(?=\s{2,}|\s+BANCO\b|\s+NU\s+INVEST\b|$)", re.IGNORECASE)
MONEY = re.compile(r"R\$\s*(?P<value>-?\d{1,3}(?:\.\d{3})*,\d+)", re.IGNORECASE)
QUANTITY_BEFORE_MONEY = re.compile(
    r"(?P<quantity>\d+(?:[.,]\d+)?)\s+R\$\s*-?\d{1,3}(?:\.\d{3})*,\d+",
    re.IGNORECASE,
)
FILTER_PERIOD = re.compile(
    r"Data\s+Inicial:\s*(?P<start>\d{2}/\d{2}/\d{4})\s*\|\s*Data\s+Final:\s*(?P<end>\d{2}/\d{2}/\d{4})",
    re.IGNORECASE,
)
MONTHS = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}


class DividendStatementError(ValueError):
    pass


def _ascii(value):
    return unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()


def _decimal(value):
    try:
        return Decimal(value.replace(".", "").replace(",", "."))
    except InvalidOperation as exc:
        raise DividendStatementError(f"Valor inválido no extrato: {value}") from exc


def _extract_text(stream):
    executable = shutil.which("pdftotext")
    if not executable:
        raise DividendStatementError("O leitor de PDF do servidor não está disponível.")
    try:
        stream.seek(0)
        with tempfile.NamedTemporaryFile(suffix=".pdf") as source:
            source.write(stream.read())
            source.flush()
            completed = subprocess.run(
                [executable, "-layout", source.name, "-"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        if completed.returncode != 0:
            raise DividendStatementError("Não foi possível extrair o texto do PDF da B3.")
        text = completed.stdout
    except Exception as exc:
        if isinstance(exc, DividendStatementError):
            raise
        raise DividendStatementError("Não foi possível abrir o extrato da B3.") from exc
    if "Extrato de Movimentação" not in text or "Proventos recebidos" not in text:
        raise DividendStatementError("Este PDF não parece ser um extrato de proventos recebidos da B3.")
    return text


def _payment_date(match):
    month = MONTHS.get(_ascii(match.group("month")).lower())
    if not month:
        raise DividendStatementError(f"Mês inválido no extrato: {match.group('month')}")
    return date(int(match.group("year")), month, int(match.group("day")))


def _clean_name(value):
    value = re.sub(r"\s+", " ", value or "").strip(" -")
    return value[:180]


def _kind(value, block):
    normalized = _ascii(value).lower()
    if normalized.startswith("juros"):
        return "Juros sobre capital próprio"
    if normalized.startswith("dividendo"):
        return "Dividendo"
    return "Rendimento"


def dividend_fingerprint(item):
    raw = "|".join((
        item["payment_date"].isoformat(), item["income_type"], item["asset"],
        item["institution"], format(item["quantity"], "f"),
        format(item["unit_value"], "f"), format(item["amount"], "f"),
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_b3_dividend_text(text):
    period = FILTER_PERIOD.search(text)
    date_matches = list(DATE_HEADING.finditer(text))
    items = []
    for date_index, date_match in enumerate(date_matches):
        segment_end = date_matches[date_index + 1].start() if date_index + 1 < len(date_matches) else len(text)
        segment = text[date_match.end():segment_end]
        movements = list(MOVEMENT_START.finditer(segment))
        for movement_index, movement in enumerate(movements):
            block_end = movements[movement_index + 1].start() if movement_index + 1 < len(movements) else len(segment)
            block = segment[movement.start():block_end]
            ticker = TICKER.search(block)
            values = [_decimal(match.group("value")) for match in MONEY.finditer(block)]
            quantity = QUANTITY_BEFORE_MONEY.search(block)
            if not ticker or len(values) < 2 or not quantity:
                continue
            flat_block = re.sub(r"\s+", " ", block)
            institution_match = re.search(
                r"\b(BANCO\s+BTG\s+PACTUAL\s+S/?A\.?|NU\s+INVEST\s+CORRETORA\s+DE\s+VALORES\s+S\.?A\.?)\b",
                flat_block,
                re.IGNORECASE,
            )
            institution = _clean_name(institution_match.group(1)) if institution_match else ""
            if not institution and "BANCO BTG" in flat_block.upper() and "PACTUAL" in flat_block.upper():
                institution = "BANCO BTG PACTUAL S/A"
            elif not institution and "NU INVEST" in flat_block.upper():
                institution = "NU INVEST CORRETORA DE VALORES S.A."
            item = {
                "payment_date": _payment_date(date_match),
                "income_type": _kind(movement.group("kind"), block),
                "asset": ticker.group("ticker").upper(),
                "asset_name": _clean_name(ticker.group("name")),
                "institution": institution,
                "quantity": _decimal(quantity.group("quantity")),
                "unit_value": values[-2],
                "amount": values[-1],
            }
            item["fingerprint"] = dividend_fingerprint(item)
            items.append(item)
    if not items:
        raise DividendStatementError("Nenhum provento foi localizado no extrato da B3.")
    return {
        "period_start": datetime.strptime(period.group("start"), "%d/%m/%Y").date() if period else min(item["payment_date"] for item in items),
        "period_end": datetime.strptime(period.group("end"), "%d/%m/%Y").date() if period else max(item["payment_date"] for item in items),
        "items": items,
    }


def parse_b3_dividend_pdf(stream):
    return parse_b3_dividend_text(_extract_text(stream))
