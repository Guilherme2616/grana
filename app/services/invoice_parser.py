import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from pypdf import PdfReader


DATE_PATTERN = re.compile(r"(?P<date>\d{2}/\d{2}(?:/\d{2,4})?)")
AMOUNT_PATTERN = re.compile(r"(?P<amount>-?\s*(?:R\$\s*)?\d{1,3}(?:\.\d{3})*,\d{2})\s*$")
EXPIRY_WORD_PATTERN = re.compile(r"\b(?:data\s+de\s+)?vencimento\b|\bvence\b", re.IGNORECASE)


class PdfPasswordRequired(ValueError):
    pass


class PdfPasswordInvalid(ValueError):
    pass


def parse_brl(value):
    cleaned = value.replace("R$", "").replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return abs(Decimal(cleaned))
    except InvalidOperation as exc:
        raise ValueError(f"Valor inválido: {value}") from exc


def parse_date(value, reference_month):
    month_year = datetime.strptime(reference_month, "%Y-%m")
    formats = ["%d/%m/%Y", "%d/%m/%y"]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    day, month = [int(part) for part in value.split("/")]
    year = month_year.year
    if month > month_year.month + 6:
        year -= 1
    return date(year, month, day)


def detect_due_date(text, reference_month):
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        if not EXPIRY_WORD_PATTERN.search(line):
            continue
        date_match = DATE_PATTERN.search(line)
        if date_match:
            try:
                return parse_date(date_match.group("date"), reference_month)
            except ValueError:
                continue
    return None


def parse_invoice_pdf(stream, reference_month, password=""):
    reader = PdfReader(stream)
    if reader.is_encrypted:
        if not password:
            raise PdfPasswordRequired("Este PDF exige uma senha.")
        if not reader.decrypt(password):
            raise PdfPasswordInvalid("A senha informada não desbloqueou o PDF.")

    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    items = []
    seen = set()

    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        date_match = DATE_PATTERN.search(line)
        amount_match = AMOUNT_PATTERN.search(line)
        if not date_match or not amount_match or date_match.start() > amount_match.start():
            continue

        description = line[date_match.end():amount_match.start()].strip(" -–—·")
        if len(description) < 2:
            continue

        try:
            purchase_date = parse_date(date_match.group("date"), reference_month)
            amount = parse_brl(amount_match.group("amount"))
        except (ValueError, InvalidOperation):
            continue

        identity = (purchase_date.isoformat(), description.lower(), str(amount))
        if identity in seen:
            continue
        seen.add(identity)
        items.append({"purchase_date": purchase_date, "description": description[:180], "amount": amount})

    return {"items": items, "due_date": detect_due_date(text, reference_month)}
