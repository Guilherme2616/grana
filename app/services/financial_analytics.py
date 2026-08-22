import calendar
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal


INSTALLMENT_TOKEN = re.compile(
    r"(?:\(?\s*parcela\s*)?\b\d{1,2}\s*(?:de|/)\s*\d{1,2}\b\s*\)?|\bparc(?:ela)?[-\s]*\d{1,2}/\d{1,2}\b",
    re.IGNORECASE,
)


def shift_month(reference_month, offset):
    year, month = map(int, reference_month.split("-"))
    absolute = year * 12 + month - 1 + offset
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


def month_label(reference_month, short=False):
    names = (
        "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
        "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
    )
    year, month = map(int, reference_month.split("-"))
    name = names[month - 1]
    return f"{name[:3] if short else name}/{str(year)[-2:] if short else year}"


def month_bounds(reference_month):
    year, month = map(int, reference_month.split("-"))
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    return start, end


def normalize_installment_description(description):
    value = INSTALLMENT_TOKEN.sub("", description or "")
    return " ".join(value.replace("(", " ").replace(")", " ").split()).strip(" -")


def build_installment_projection(invoice_items, base_month, months=12):
    """Projeta somente a ocorrência mais recente de cada parcelamento."""
    latest = {}
    for item in invoice_items:
        if not item.installment_total or not item.installment_current:
            continue
        if item.installment_current >= item.installment_total:
            continue
        identity = (
            item.invoice.card_id,
            normalize_installment_description(item.description).lower(),
            str(Decimal(item.amount).quantize(Decimal("0.01"))),
        )
        rank = (item.invoice.reference_month, item.installment_current, item.id or 0)
        if identity not in latest or rank > latest[identity][0]:
            latest[identity] = (rank, item)

    projection = defaultdict(list)
    last_month = shift_month(base_month, months)
    for _, item in latest.values():
        remaining = item.installment_total - item.installment_current
        for step in range(1, remaining + 1):
            target_month = shift_month(item.invoice.reference_month, step)
            if target_month <= base_month or target_month > last_month:
                continue
            projection[target_month].append({
                "card_id": item.invoice.card_id,
                "description": normalize_installment_description(item.description),
                "amount": Decimal(item.amount),
                "installment_current": item.installment_current + step,
                "installment_total": item.installment_total,
            })
    return projection
