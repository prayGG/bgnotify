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


def _fmt_eur(value: float, approx: bool = False) -> str:
    """Betrag in deutscher Schreibweise: Symbol HINTEN, Komma als Dezimaltrenner,
    Punkt als Tausendertrenner — `1.234,56 €`. `approx` stellt ein `≈` voran
    (für aus USD umgerechnete Preise)."""
    s = f"{value:,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")
    return f"{'≈' if approx else ''}{s} €"


def display_price(raw: str, rate: Optional[float]) -> str:
    """Preis-String für die Anzeige: deutsches Format `21,58 €`. USD wird via
    `rate` zu `≈21,58 €` umgerechnet; ohne Kurs bleibt ein USD-Preis roh."""
    if not raw:
        return ""
    m = _USD_PATTERN.search(raw)
    if m:
        if rate is None:
            return raw  # USD ohne Kurs — nicht umrechenbar, roh lassen
        try:
            return _fmt_eur(float(m.group(1).replace(",", "")) * rate, approx=True)
        except ValueError:
            return raw
    val = price_value(raw)
    return _fmt_eur(val) if val is not None else raw


def fmt_price_value(value: float, sample: str, rate: Optional[float]) -> str:
    """Numerischen Preis im deutschen Anzeigeformat (`21,58 €`). Ist `sample` ein
    USD-Preis, wird via `rate` umgerechnet."""
    if "$" in (sample or ""):
        return display_price(f"${value:.2f}", rate)
    return _fmt_eur(value)


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
