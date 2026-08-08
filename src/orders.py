"""BG-Kundenkonto: Bestellstatus + Hermes-Tracking auslesen.

Quelle ist das WooCommerce-Kundenkonto (Free-Proton ist für einen Bot nicht
lesbar — siehe Projektnotizen). Ablauf:

1. Login via Playwright (headless Chromium, wie `forum.py` — kommt an Incapsula
   vorbei, sobald die Session steht).
2. `/my-account/orders/` → Liste aller Bestellungen mit Status-Slug.
3. Pro relevanter Bestellung `/my-account/view-order/<id>/` → Hermes-Tracking
   aus den Order-Notes.

Dieses Modul macht NUR Fetch + Parse. Diffing gegen den letzten Stand (privates
Gist) und das Discord-Posten liegen bei den Aufrufern, damit die Parser pur und
ohne Browser testbar bleiben.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE = "https://bgpharmadrugs.to"
ACCOUNT_URL = f"{BASE}/my-account/"
ORDERS_URL = f"{BASE}/my-account/orders/"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Status-Slug steckt in der Zeilen-Klasse: ...__row--status-completed
_ROW_STATUS_RE = re.compile(r"--status-([a-z0-9\-]+)")
_ORDER_ID_RE = re.compile(r"/view-order/(\d+)")
# Tracking-Links, die wir als Sendungsverfolgung erkennen. BG nutzt aktuell
# Hermes — die übrigen sind ein Sicherheitsnetz, falls der Carrier wechselt.
# Substring-Match auf die URL; bewusst spezifisch gehalten (keine Fehl-Treffer).
_TRACKING_HOSTS = (
    "hermesworld", "myhermes", "evri",          # Hermes (aktuell)
    "dhl.", "dpd.", "gls-group", "gls.de",      # DHL / DPD / GLS
    "deutschepost", "ups.com", "fedex.com",     # Dt. Post / UPS / FedEx
    "17track", "tracking.",                      # Aggregator / generischer Tracking-Host
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# --------------------------------------------------------------------------
# Parsing (pur — gegen gespeichertes HTML testbar)
# --------------------------------------------------------------------------
def parse_orders_list(html_text: str) -> list[dict]:
    """Bestellungen aus `/my-account/orders/` extrahieren.

    Liefert je Bestellung: order_id, status (Slug), status_text, date_iso, url.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    out: list[dict] = []
    for row in soup.select("tr.woocommerce-orders-table__row"):
        link = row.select_one(".woocommerce-orders-table__cell-order-number a")
        href = link.get("href", "") if link else ""
        m = _ORDER_ID_RE.search(href)
        if not m:
            continue
        order_id = m.group(1)

        slug = ""
        for cls in row.get("class", []):
            sm = _ROW_STATUS_RE.search(cls)
            if sm:
                slug = sm.group(1)
                break

        status_cell = row.select_one(".woocommerce-orders-table__cell-order-status")
        time_el = row.select_one("time[datetime]")
        out.append({
            "order_id": order_id,
            "status": slug,
            "status_text": _norm(status_cell.get_text()) if status_cell else "",
            "date_iso": (time_el.get("datetime") if time_el else "") or "",
            "url": urljoin(BASE, href),
        })
    return out


def parse_order_detail(html_text: str) -> dict:
    """Status + Tracking-Links von `/my-account/view-order/<id>/` extrahieren.

    Tracking = Order-Note, deren Beschreibung einen Link auf einen bekannten
    Versand-Tracking-Host enthält (BG: tracking.hermesworld.com).
    """
    soup = BeautifulSoup(html_text, "html.parser")

    status_el = soup.select_one("mark.order-status")
    number_el = soup.select_one("mark.order-number")

    tracking: list[str] = []
    for note in soup.select("ol.woocommerce-OrderUpdates li.note .woocommerce-OrderUpdate-description a"):
        href = (note.get("href") or "").strip()
        if href and any(h in href for h in _TRACKING_HOSTS):
            if href not in tracking:
                tracking.append(href)

    # Bestellte Artikel (Name + Menge) aus der Order-Details-Tabelle.
    items: list[str] = []
    for row in soup.select("tr.woocommerce-table__line-item, tr.order_item"):
        name_cell = row.select_one(".product-name")
        if not name_cell:
            continue
        link = name_cell.select_one("a")
        name = _norm(link.get_text()) if link else _norm(name_cell.get_text())
        qty_el = name_cell.select_one(".product-quantity")
        qty = _norm(qty_el.get_text()) if qty_el else ""
        line = _norm(f"{name} {qty}")
        if line:
            items.append(line)

    return {
        "order_id": _norm(number_el.get_text()) if number_el else "",
        "status_text": _norm(status_el.get_text()) if status_el else "",
        "tracking": tracking,
        "items": items,
    }


# --------------------------------------------------------------------------
# Fetch (Playwright — deferred import wie in forum.py)
# --------------------------------------------------------------------------
def fetch(username: str, password: str, want_detail=None, cookies=None) -> tuple[list[dict], dict, list]:
    """Bestellliste + gewünschte Detailseiten holen — Session wiederverwendend.

    `cookies` (aus einem früheren Lauf) wird zuerst geladen; ist die Session noch
    gültig, sparen wir uns das Login-Formular komplett. Nur wenn die Session weg/
    abgelaufen ist, wird mit "Angemeldet bleiben" frisch eingeloggt (Session hält
    dann ~14 Tage). So passieren echte Logins nur noch selten — viel unauffälliger.

    `want_detail(order)->bool` entscheidet pro Bestellung, ob ihr Detail geladen
    wird. Returns: (orders, details, cookies) — die (ggf. erneuerten) Cookies zum
    Persistieren. Wirft RuntimeError, wenn der Login nicht greift.
    """
    from playwright.sync_api import sync_playwright  # noqa: WPS433

    details: dict = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            ctx = browser.new_context(
                user_agent=UA,
                locale="de-DE",
                # Wie in hermes.py: ohne das läuft der Browser auf UTC (Runner)
                # und alle im Browser formatierten Zeiten wären zwei Stunden
                # daneben. Nebenbei passt es zum de-DE-Auftritt der Session.
                timezone_id="Europe/Berlin",
                viewport={"width": 1280, "height": 800},
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            if cookies:
                try:
                    ctx.add_cookies(cookies)
                except Exception as e:  # kaputte/abgelaufene Cookies → einfach neu einloggen
                    log.warning("gespeicherte Cookies ungültig (%s) — logge neu ein", e)

            page = ctx.new_page()
            # Direkt zur Bestellliste: mit gültiger Session sind wir schon drin.
            page.goto(ORDERS_URL, wait_until="networkidle", timeout=45000)

            if _is_logged_in(page.content()):
                log.info("orders: Session wiederverwendet (kein Login nötig)")
            else:
                # Session weg/abgelaufen → frisch einloggen, mit "Angemeldet bleiben".
                page.goto(ACCOUNT_URL, wait_until="networkidle", timeout=45000)
                page.wait_for_selector("input#username", timeout=15000)
                page.fill("input#username", username)
                page.fill("input#password", password)
                try:
                    page.check("input#rememberme")
                except Exception:
                    pass
                page.click("button[name='login']")
                page.wait_for_load_state("networkidle", timeout=45000)
                if not _is_logged_in(page.content()):
                    raise RuntimeError("BG login failed (check credentials / Incapsula)")
                page.goto(ORDERS_URL, wait_until="networkidle", timeout=45000)
                log.info("orders: frischer Login (Session erneuert)")

            orders = parse_orders_list(page.content())
            for o in orders:
                if want_detail and want_detail(o):
                    page.goto(o["url"], wait_until="networkidle", timeout=45000)
                    details[o["order_id"]] = page.content()

            return orders, details, ctx.cookies()
        finally:
            browser.close()


def _is_logged_in(html_text: str) -> bool:
    return "woocommerce-MyAccount-navigation" in html_text or "customer-logout" in html_text


# --------------------------------------------------------------------------
# Order-Stand: privates GitHub-Gist (state.json ist öffentlich → tabu)
# --------------------------------------------------------------------------
GIST_FILE = "order-state.json"
_GH_API = "https://api.github.com/gists"


def _gist_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def load_order_state(token: str, gist_id: str) -> dict:
    """Order-Stand aus dem privaten Gist lesen. {} bei leer/Fehler."""
    import json as _json
    try:
        r = requests.get(f"{_GH_API}/{gist_id}", headers=_gist_headers(token), timeout=15)
        r.raise_for_status()
        content = (r.json().get("files", {}).get(GIST_FILE, {}) or {}).get("content", "")
        return _json.loads(content) if content.strip() else {}
    except (requests.RequestException, ValueError) as e:
        log.error("Gist-Load fehlgeschlagen: %s", e)
        return {}


def save_order_state(token: str, gist_id: str, state: dict) -> bool:
    """Order-Stand ins private Gist schreiben."""
    import json as _json
    payload = {"files": {GIST_FILE: {"content": _json.dumps(state, indent=2, ensure_ascii=False)}}}
    try:
        r = requests.patch(f"{_GH_API}/{gist_id}", headers=_gist_headers(token), json=payload, timeout=15)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("Gist-Save fehlgeschlagen: %s", e)
        return False
