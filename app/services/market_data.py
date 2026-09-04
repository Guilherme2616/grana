from decimal import Decimal, InvalidOperation

import requests


BRAPI_QUOTE_URL = "https://brapi.dev/api/v2/stocks/quote"


class MarketDataError(RuntimeError):
    """Raised when a market-data provider cannot return a usable quote."""


def fetch_brapi_quote(asset, token, timeout=8):
    symbol = (asset or "").strip().upper()
    if not symbol:
        raise MarketDataError("Ativo inválido.")
    if not token:
        raise MarketDataError("Configure a chave da Brapi para atualizar as cotações.")

    try:
        response = requests.get(
            BRAPI_QUOTE_URL,
            params={"symbols": symbol},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "Grana/1.0",
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise MarketDataError("A Brapi está indisponível no momento.") from exc

    if response.status_code in {401, 403}:
        raise MarketDataError("A chave da Brapi não foi aceita.")
    if response.status_code == 429:
        raise MarketDataError("O limite mensal de consultas da Brapi foi atingido.")
    if response.status_code >= 400:
        raise MarketDataError("Não foi possível consultar a cotação na Brapi.")

    try:
        payload = response.json()
        result = (payload.get("results") or [])[0]
        data = result.get("data") or result
        price = Decimal(str(data["regularMarketPrice"]))
        change = data.get("regularMarketChangePercent")
        change_percent = Decimal(str(change)) if change is not None else None
    except (IndexError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise MarketDataError(f"A Brapi não encontrou uma cotação válida para {symbol}.") from exc

    if price <= 0:
        raise MarketDataError(f"A Brapi não encontrou uma cotação válida para {symbol}.")

    return {
        "asset": (result.get("symbol") or symbol).upper(),
        "name": data.get("shortName") or data.get("longName") or "",
        "price": price.quantize(Decimal("0.01")),
        "change_percent": change_percent,
    }
