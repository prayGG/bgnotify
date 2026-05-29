"""Einmaliger Machbarkeits-Test: Kommt der Login ins BG-Kundenkonto durch
Incapsula, und erreichen wir danach die Bestellliste?

KEIN Teil des Bots — nur ein Spike. Wenn er PASS meldet, bauen wir den
richtigen Scraper (`src/orders.py`). Zugangsdaten kommen aus Umgebungs-
variablen, damit nichts in einer Datei oder im Repo landet:

    $env:BG_USERNAME = "deine-bg-mail"
    $env:BG_PASSWORD = "dein-passwort"
    python -m src.orders_login_spike

Optional sichtbarer Browser zum Zuschauen:  $env:BG_HEADFUL = "1"

Speichert die Bestelllisten-HTML lokal als `order-list.html` (gitignored),
damit der Listen-Parser danach passend gebaut werden kann.
"""
from __future__ import annotations

import logging
import os
import sys

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE = "https://bgpharmadrugs.to"
ACCOUNT_URL = f"{BASE}/my-account/"
ORDERS_URL = f"{BASE}/my-account/orders/"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _looks_logged_in(html_text: str) -> bool:
    """Logged-in account pages carry the MyAccount nav with a Log out link."""
    soup = BeautifulSoup(html_text, "html.parser")
    if soup.select_one(".woocommerce-MyAccount-navigation"):
        return True
    return "customer-logout" in html_text


def run(username: str, password: str, headful: bool) -> int:
    from playwright.sync_api import sync_playwright  # deferred like forum.py

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headful,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            ctx = browser.new_context(
                user_agent=UA,
                locale="de-DE",
                viewport={"width": 1280, "height": 800},
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = ctx.new_page()

            log.info("→ lade %s (Incapsula braucht networkidle)", ACCOUNT_URL)
            page.goto(ACCOUNT_URL, wait_until="networkidle", timeout=45000)

            if _looks_logged_in(page.content()):
                log.info("schon eingeloggt (Cookie aus vorherigem Lauf?)")
            else:
                # WooCommerce-Standard-Loginform: #username / #password / button[name=login]
                try:
                    page.wait_for_selector("input#username", timeout=15000)
                except Exception:
                    log.error("FAIL: Loginformular nicht gefunden — evtl. Incapsula-Challenge "
                              "hängt. order-list.html zum Anschauen gespeichert.")
                    _dump(page.content())
                    return 1

                log.info("→ fülle Login aus und sende ab")
                page.fill("input#username", username)
                page.fill("input#password", password)
                page.click("button[name='login']")
                page.wait_for_load_state("networkidle", timeout=45000)

                content = page.content()
                if not _looks_logged_in(content):
                    # WooCommerce zeigt Fehler in .woocommerce-error
                    soup = BeautifulSoup(content, "html.parser")
                    err = soup.select_one(".woocommerce-error, .woocommerce-notices-wrapper")
                    msg = err.get_text(" ", strip=True)[:200] if err else "(keine Fehlermeldung gefunden)"
                    log.error("FAIL: Login nicht erfolgreich. Seite sagt: %s", msg)
                    _dump(content)
                    return 1

            log.info("✓ eingeloggt — lade Bestellliste %s", ORDERS_URL)
            page.goto(ORDERS_URL, wait_until="networkidle", timeout=45000)
            content = page.content()
            _dump(content)

            soup = BeautifulSoup(content, "html.parser")
            rows = soup.select(
                "tr.woocommerce-orders-table__row, "
                ".woocommerce-orders-table tbody tr, "
                ".woocommerce-MyAccount-content table tbody tr"
            )
            view_links = [a.get("href", "") for a in soup.select("a")
                          if "view-order/" in (a.get("href", "") or "")]

            log.info("─" * 40)
            if rows or view_links:
                log.info("✓✓ PASS — Bestellliste erreicht.")
                log.info("   Tabellen-Zeilen gefunden: %d", len(rows))
                log.info("   view-order-Links gefunden: %d", len(view_links))
                log.info("   HTML gespeichert: order-list.html (schick mir die, "
                         "dann bau ich den Listen-Parser)")
                return 0
            log.warning("? Login OK, aber keine Bestellzeilen erkannt — evtl. keine "
                        "Bestellungen, oder andere Tabellenstruktur. order-list.html prüfen.")
            return 0
        finally:
            browser.close()


def _dump(html_text: str) -> None:
    """Lokal speichern für die Parser-Entwicklung (order-list.html ist gitignored)."""
    try:
        with open("order-list.html", "w", encoding="utf-8") as fh:
            fh.write(html_text)
    except OSError as e:
        log.warning("konnte order-list.html nicht schreiben: %s", e)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    user = os.environ.get("BG_USERNAME", "").strip()
    pw = os.environ.get("BG_PASSWORD", "").strip()
    if not user or not pw:
        log.error("Bitte BG_USERNAME und BG_PASSWORD als Umgebungsvariablen setzen.")
        return 2
    headful = os.environ.get("BG_HEADFUL", "") not in ("", "0", "false")
    return run(user, pw, headful)


if __name__ == "__main__":
    sys.exit(main())
