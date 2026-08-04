"""Hermes-Sendungsverfolgung: aktuellen Status zu einer Tracking-URL holen.

Ein offizielles API gibt es nicht — Hermes' Track&Trace-Schnittstelle setzt einen
Vertrag voraus. Deshalb wird die öffentliche Sendungsseite mit Playwright geladen
(sie rendert die Ereignisse per JS nach) und der jüngste Eintrag geparst.

Bewusst defensiv: mehrere Erkennungswege, und wenn keiner greift, `None` statt
Raten. So postet der Bot lieber nichts, als etwas Falsches.

Debug:
    python -m src.hermes "https://tracking.hermesworld.com/?TrackID=..."
"""
from __future__ import annotations

import logging
import re
import sys
from typing import Optional

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Bekannte Status-Formulierungen, grob von "früh" nach "spät". Wird als
# Fallback genutzt, wenn kein strukturierter Eintrag gefunden wird.
_STATUS_PHRASES = [
    "sendung angekündigt", "sendung avisiert", "auftrag erfasst",
    "im paketzentrum", "im logistikzentrum", "sortiert",
    "auf dem weg", "unterwegs", "in zustellung", "im zustellfahrzeug",
    "im paketshop", "abholbereit", "zur abholung",
    "zugestellt", "ausgeliefert", "empfangen",
    "nicht angetroffen", "zurückgesendet", "retoure",
]

_TERMINAL = ("zugestellt", "zustellung erfolgt", "ausgeliefert", "empfangen",
             "zurückgesendet", "retoure")


def is_terminal(status: str) -> bool:
    """Sendung abgeschlossen? Dann muss nicht weiter gepollt werden."""
    s = (status or "").lower()
    return any(t in s for t in _TERMINAL)


def _fetch_text(url: str, timeout_ms: int = 45000) -> str:
    """Sendungsseite rendern und den sichtbaren Text zurückgeben."""
    from playwright.sync_api import sync_playwright  # noqa: WPS433

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            ctx = browser.new_context(
                user_agent=UA, locale="de-DE",
                viewport={"width": 1280, "height": 900},
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = ctx.new_page()
            # networkidle: die Ereignisliste kommt per XHR nach.
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            try:
                page.wait_for_timeout(1500)  # letzte Nachzügler
            except Exception:
                pass
            return page.inner_text("body")
        finally:
            browser.close()


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def parse_status(text: str) -> Optional[str]:
    """Jüngsten Status aus dem Seitentext ziehen.

    1. Zeile mit Datum + Text (typische Ereigniszeile) — die oberste gewinnt,
       Hermes listet neueste zuerst.
    2. Sonst: erste bekannte Status-Formulierung im Text.
    """
    lines = [_clean(l) for l in (text or "").splitlines()]
    lines = [l for l in lines if l]

    # 1) Ereigniszeile: beginnt mit Datum (12.08.2026 / 12.08.26 / 12. August).
    #    Hermes listet neueste zuerst → die oberste gewinnt.
    date_re = re.compile(
        r"^\d{1,2}[.\-/]\s?\d{1,2}[.\-/]\s?\d{2,4}"          # 12.08.2026
        r"|^\d{1,2}\.\s*[A-Za-zäöüÄÖÜ]+\.?(\s*\d{2,4})?",     # 12. August 2026
        re.I,
    )
    time_re = re.compile(r"^\s*\d{1,2}[:.]\d{2}(\s*Uhr)?\b", re.I)
    for i, line in enumerate(lines):
        if not date_re.match(line):
            continue
        # Datum und ggf. Uhrzeit abschneiden — was übrig bleibt, ist der Status.
        rest = time_re.sub("", date_re.sub("", line)).strip(" ,;–-|")
        if len(rest) > 3:
            return rest[:200]
        # Zeile enthielt nur Datum/Uhrzeit → Status steht in der nächsten Zeile.
        for nxt in lines[i + 1:i + 3]:
            cand = time_re.sub("", nxt).strip(" ,;–-|")
            if len(cand) > 3 and not date_re.match(cand):
                return cand[:200]
        break

    # 2) Bekannte Formulierung
    low = (text or "").lower()
    for phrase in _STATUS_PHRASES:
        idx = low.find(phrase)
        if idx >= 0:
            for line in lines:
                if phrase in line.lower():
                    return line[:200]
            return phrase

    return None


def fetch_status(url: str) -> Optional[str]:
    """Aktuellen Sendungsstatus oder None (Seite nicht lesbar / Format unbekannt)."""
    try:
        text = _fetch_text(url)
    except Exception as e:
        log.warning("hermes: Seite nicht ladbar (%s): %s", url[:60], e)
        return None
    status = parse_status(text)
    if not status:
        log.warning("hermes: kein Status erkennbar — Layout geändert? (%s)", url[:60])
    return status


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: python -m src.hermes <tracking-url>")
        raise SystemExit(2)
    raw = _fetch_text(sys.argv[1])
    print("--- Seitentext (erste 2000 Zeichen) ---")
    print(raw[:2000])
    print("--- erkannter Status ---")
    print(parse_status(raw))
