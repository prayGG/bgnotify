"""Preis- und Währungs-Helfer.

Der Shop liefert je nach Runner-IP USD- oder EUR-Preise; angezeigt wird immer
EUR. Hier leben der Tageskurs (Frankfurter/ECB), das Parsen von Preis-Strings
in Zahlen und die Formatierung zurück in Anzeige-Strings.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import requests

log = logging.getLogger(__name__)

_USD_PATTERN = re.compile(r"\$\s*([\d,]+\.?\d*)")
_PRICE_VALUE_PATTERN = re.compile(r"([\d,]+\.?\d*)")
_CURRENCY_SYMBOL = {"EUR": "€", "USD": "$", "GBP": "£"}


def fetch_usd_eur_rate() -> Optional[float]:
    """Daily USD->EUR rate from Frankfurter (ECB-backed, no key). None on error."""
    try:
        r = requests.get("https://api.frankfurter.app/latest?from=USD&to=EUR", timeout=10)
        r.raise_for_status()
        return float(r.json()["rates"]["EUR"])
    except Exception as e:
        log.warning("USD/EUR rate fetch failed: %s", e)
        return None


def price_value(raw: str) -> Optional[float]:
    """Extract numeric amount from any price string (handles `$X.XX`, `€ X.XX`)."""
    if not raw:
        return None
    m = _PRICE_VALUE_PATTERN.search(raw.replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def display_price(raw: str, rate: Optional[float]) -> str:
    """Convert `$X.XX` to `≈€Y.YY` if a USD price + rate available; pass through otherwise."""
    if not raw:
        return ""
    m = _USD_PATTERN.search(raw)
    if not m or rate is None:
        return raw
    try:
        usd = float(m.group(1).replace(",", ""))
    except ValueError:
        return raw
    return f"≈€{usd * rate:.2f}"


def fmt_price_value(value: float, sample: str, rate: Optional[float]) -> str:
    """Format a numeric price using the currency style of `sample` (USD→EUR via rate)."""
    if "$" in (sample or ""):
        return display_price(f"${value:.2f}", rate)
    if "€" in (sample or ""):
        return f"€{value:.2f}"
    return f"{value:.2f}"


def fmt_price_cents(cents: Optional[int], currency: str = "EUR") -> str:
    """Cents → Anzeigepreis im SELBEN Format wie die Shop-Produkte ("€ 59.99":
    Symbol + Space + Punkt-Dezimal). Aus den numerischen Werten formatiert (nicht
    aus dem gescrapten String), damit das € unabhängig von der Encoding-Umgebung
    stimmt UND `price_value`/`display_price` es wie einen Medi-Preis lesen."""
    if cents is None:
        return ""
    sym = _CURRENCY_SYMBOL.get(currency)
    if sym:
        return f"{sym} {cents / 100:.2f}"
    return f"{currency + ' ' if currency else ''}{cents / 100:.2f}"
