"""Check stock status of WooCommerce products on bgpharmadrugs.to.

Supports two product types automatically:

1. **Variable products** (e.g. /product/peptides/ with a peptide-name dropdown):
   The page has 50+ variants which exceeds WC's `wc_ajax_variation_threshold`,
   so `data-product_variations` is `"false"` and the frontend looks up each
   chosen variant via AJAX. We POST `?wc-ajax=get_variation` with the chosen
   attribute value and read `is_in_stock`, `is_purchasable` from the response.

2. **Simple products** (e.g. /product/roaccutane-20-mg-30-roche/): no
   dropdown — the URL IS the product. We detect stock from the body class
   (`outofstock` toggled by WooCommerce), the `<p class="stock">` badge, and
   the presence of an "Add to cart" button.
"""
from __future__ import annotations

import concurrent.futures
import html
import json
import logging
import re
import sys
import time
from typing import Optional
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Per-run cache of *parsed* product pages, keyed by URL. Several configured
# products share one page (e.g. every peptide lives on /product/peptides/), so
# without this each one would re-GET *and* re-parse the same (large) HTML and
# rebuild the same 50+ option variation form. We cache the parsed representation
# `(soup, is_simple, form)` so a shared page is fetched and parsed exactly once
# per run. The caller resets it via reset_page_cache() so data never goes stale
# across runs.
_PAGE_CACHE: dict[str, tuple] = {}

# Rohes HTML aus `prefetch()`, das `check()` gleich abholt statt selbst zu GETen.
# Bewusst getrennt vom _PAGE_CACHE: geparst wird weiter im Hauptthread (BeautifulSoup
# ist CPU-Arbeit, die unter dem GIL ohnehin nicht parallel läuft) — überlappt wird
# nur das Warten auf den Shop.
_HTML_CACHE: dict[str, str] = {}

# Gleichzeitige GETs beim Vorladen. Klein gehalten: es sind ohnehin nur eine
# Handvoll verschiedener Seiten und der Shop soll keinen Burst abbekommen.
_PREFETCH_WORKERS = 4

# One HTTP session per run. Keep-alive + connection pooling means the page GETs
# and the per-variant AJAX POSTs to the shop reuse a single TCP/TLS connection
# instead of paying a fresh handshake on every call (the peptides page alone
# fires one AJAX request per watched variant).
_SESSION: Optional[requests.Session] = None


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
    return _SESSION


def reset_page_cache() -> None:
    """Drop the per-run page cache + session. Call once at the start of a run."""
    global _SESSION
    _PAGE_CACHE.clear()
    _HTML_CACHE.clear()
    if _SESSION is not None:
        _SESSION.close()
        _SESSION = None


def prefetch(urls: list[str]) -> None:
    """Alle Produktseiten vorab parallel holen (best effort).

    Vorher lief das streng nacheinander: jede Seite zahlte die volle Latenz zum
    Shop, obwohl die Requests völlig unabhängig sind. Jetzt überlappen sie sich,
    der Rest des Laufs findet das HTML fertig im Cache vor.

    Bewusst fehlertolerant: Was hier schiefgeht, wird NICHT geloggt und nicht
    gecacht — `check()` holt die Seite dann ganz normal selbst, inklusive Retry,
    Fehlerbehandlung und der gewohnten Log-Zeile. Ein kaputter Prefetch kann den
    Lauf also höchstens so langsam machen wie vorher, nie kaputt.

    Jeder Thread bekommt seine eigene Session — `requests.Session` ist laut
    Dokumentation nicht thread-safe, die gemeinsame bleibt dem Hauptthread
    (und seinen Varianten-AJAX-Calls) vorbehalten.
    """
    todo = [u for u in dict.fromkeys(urls) if u not in _PAGE_CACHE and u not in _HTML_CACHE]
    if len(todo) < 2:
        return  # bei einer einzigen Seite gibt es nichts zu überlappen

    def grab(url: str) -> tuple[str, Optional[str]]:
        try:
            with requests.Session() as s:
                return url, _fetch(url, s)
        except Exception:
            return url, None  # still verwerfen — check() macht es ordentlich

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(_PREFETCH_WORKERS, len(todo))) as pool:
        for url, page in pool.map(grab, todo):
            if page is not None:
                _HTML_CACHE[url] = page
    log.info("prefetch: %d/%d Seite(n) vorgeladen", len(_HTML_CACHE), len(todo))


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _not_found(deep_link: str = "") -> dict:
    return {"found": False, "in_stock": False, "price": "", "variation_id": None, "deep_link": deep_link}


def _deep_link(product_url: str, attr_name: str = "", option_value: str = "") -> str:
    """Return a URL that pre-selects the variant. Falls back to product URL for simple products."""
    if not attr_name or not option_value:
        return product_url
    sep = "&" if "?" in product_url else "?"
    return f"{product_url}{sep}{attr_name}={quote(option_value)}"


def _force_eur_kwargs() -> dict:
    """GitHub Actions runners hit BG Pharma from US IPs, which auto-switches the
    shop to USD. We override by sending cookies for the most common WooCommerce
    currency-switcher plugins (one of them sticks) plus a `currency=EUR` query.
    """
    cookies = {
        "aelia_cs_selected_currency": "EUR",
        "wmc_current_currency": "EUR",
        "curcy_currency": "EUR",
        "WOOCS_CURRENT_CURRENCY": "EUR",
        "yith_wcmcs_currency": "EUR",
        "wc_currency": "EUR",
    }
    headers = {"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.7"}
    return {"headers": headers, "cookies": cookies, "timeout": 20}


def _with_currency_param(url: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}currency=EUR"


def _fetch(url: str, session: requests.Session) -> str:
    """GET with retries for transient errors only (timeouts, 5xx, network).

    4xx responses are deterministic client errors (e.g. a 404 for a removed
    product page) — retrying just burns backoff sleeps on something that will
    never succeed, so those raise immediately.
    """
    last_err: Optional[Exception] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            r = session.get(_with_currency_param(url), **_force_eur_kwargs())
        except requests.RequestException as e:
            last_err = e
            if attempt < _MAX_ATTEMPTS - 1:
                wait = 2 ** attempt
                log.warning("fetch %s network error, retry in %ds: %s", url, wait, e)
                time.sleep(wait)
                continue
            raise
        if 500 <= r.status_code < 600 and attempt < _MAX_ATTEMPTS - 1:
            wait = 2 ** attempt
            log.warning("fetch %s → %s, retry in %ds", url, r.status_code, wait)
            time.sleep(wait)
            continue
        r.raise_for_status()  # 4xx → raise now (no retry); 2xx → return body
        return r.text
    if last_err:
        raise last_err
    raise RuntimeError(f"fetch {url} exhausted retries")


def _body_classes(soup: BeautifulSoup) -> list[str]:
    body = soup.find("body")
    return body.get("class") if body and body.get("class") else []


def _is_simple_product(soup: BeautifulSoup) -> bool:
    classes = _body_classes(soup)
    if "product-type-simple" in classes:
        return True
    if "product-type-variable" in classes:
        return False
    # Fallback: no variations form means it's not a variable product.
    return soup.find("form", class_="variations_form") is None


def _extract_price(soup: BeautifulSoup) -> str:
    el = soup.select_one("p.price, .price .woocommerce-Price-amount, .price")
    if not el:
        return ""
    return _strip_html(el.decode_contents())


def _check_simple_from_soup(soup: BeautifulSoup, url: str) -> dict:
    classes = _body_classes(soup)
    out_of_stock = "outofstock" in classes

    if not out_of_stock:
        stock_el = soup.find(class_=re.compile(r"\bstock\b"))
        if stock_el and "out-of-stock" in (stock_el.get("class") or []):
            out_of_stock = True

    add_to_cart = soup.find("button", attrs={"name": "add-to-cart"}) or soup.find(
        "button", class_=re.compile(r"single_add_to_cart_button")
    )

    in_stock = (not out_of_stock) and (add_to_cart is not None)
    return {
        "found": True,
        "in_stock": in_stock,
        "price": _extract_price(soup),
        "variation_id": None,
        "deep_link": url,
    }


def _parse_variable_form(soup: BeautifulSoup) -> tuple[Optional[int], list[dict], dict[str, dict[str, str]]]:
    form = soup.find("form", class_="variations_form")
    if not form:
        return None, [], {}

    pid_raw = form.get("data-product_id") or ""
    product_id = int(pid_raw) if pid_raw.isdigit() else None

    inline: list[dict] = []
    raw = form.get("data-product_variations") or ""
    if raw and raw not in ("false", "[]"):
        try:
            data = json.loads(html.unescape(raw))
            if isinstance(data, list):
                inline = data
        except json.JSONDecodeError as e:
            log.warning("inline variations JSON failed to parse: %s", e)

    attr_options: dict[str, dict[str, str]] = {}
    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        opts: dict[str, str] = {}
        for o in sel.find_all("option"):
            val = (o.get("value") or "").strip()
            if not val:
                continue
            label = (o.get_text() or "").strip() or val
            opts[_normalize(label)] = val
            opts[_normalize(val)] = val
        attr_options[name] = opts

    return product_id, inline, attr_options


def _variation_price(v: dict) -> str:
    """Anzeigepreis einer Varianten-dict (inline-JSON ODER AJAX-Antwort).

    Bevorzugt das fertig gerenderte `price_html` (trägt das Währungssymbol).
    Bei kleinen variablen Produkten (≤ WC-Threshold) liegen die Varianten inline
    im Seiten-JSON, wo `price_html` aber oft LEER ist — dann nehmen wir den
    numerischen `display_price` und formatieren ihn als EUR (der Scraper erzwingt
    via Cookies durchgängig EUR, genau wie im price_html-Pfad)."""
    raw = _strip_html(v.get("price_html") or "")
    if raw:
        return raw
    amount = v.get("display_price")
    if amount is None:
        amount = v.get("display_regular_price")
    if amount is None:
        return ""
    try:
        return f"€{float(amount):.2f}"
    except (TypeError, ValueError):
        return ""


def _match_inline(variations: list[dict], wanted: str) -> Optional[dict]:
    needle = _normalize(wanted)
    for v in variations:
        label = _normalize(" ".join(str(x) for x in (v.get("attributes") or {}).values() if x))
        if needle == label or needle in label or label in needle:
            return v
    return None


def _lookup_option_value(attr_options: dict[str, dict[str, str]], wanted: str) -> Optional[tuple[str, str]]:
    needle = _normalize(wanted)
    for attr_name, opts in attr_options.items():
        if needle in opts:
            return attr_name, opts[needle]
        for label, val in opts.items():
            if needle in label or label in needle:
                return attr_name, val
    return None


class AjaxError(Exception):
    """Transient ajax failure (HTTP/network/JSON) — distinct from a legitimate
    `null` response meaning 'no variation matched'."""


def _ajax_variation(
    session: requests.Session,
    base_url: str,
    product_id: int,
    attr_name: str,
    option_value: str,
    referer: str,
) -> Optional[dict]:
    """Return variation dict, or None if WooCommerce confirmed no match.
    Raises AjaxError on transient failures (HTTP error, network, bad JSON)."""
    ajax = f"{base_url}/?wc-ajax=get_variation&currency=EUR"
    kwargs = _force_eur_kwargs()
    kwargs["headers"] = {
        **kwargs["headers"],
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }

    last_err: Optional[Exception] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            r = session.post(
                ajax,
                data={"product_id": product_id, attr_name: option_value},
                **kwargs,
            )
        except requests.RequestException as e:
            last_err = AjaxError(f"network error: {e}")
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise last_err from e
        if 500 <= r.status_code < 600 and attempt < _MAX_ATTEMPTS - 1:
            log.warning("ajax → %s, retrying", r.status_code)
            time.sleep(2 ** attempt)
            continue
        if r.status_code >= 400:
            raise AjaxError(f"http {r.status_code}")
        if not r.content:
            raise AjaxError("empty body")
        try:
            data = r.json()
        except json.JSONDecodeError as e:
            raise AjaxError(f"bad json: {e}") from e
        if data is False or data is None:
            return None  # legitimate "no variation matched"
        if isinstance(data, dict) and "variation_id" in data:
            return data
        raise AjaxError(f"unexpected payload shape: {type(data).__name__}")
    if last_err:
        raise last_err
    raise AjaxError("ajax retries exhausted")


def _check_variable(
    session: requests.Session,
    url: str,
    watch_variants: list[str],
    form: tuple,
) -> dict[str, dict]:
    product_id, inline, attr_options = form
    if product_id is None:
        log.warning("no variations_form / product_id on %s", url)
        return {n: _not_found() for n in watch_variants}

    base = _origin(url)
    out: dict[str, dict] = {}

    for name in watch_variants:
        if inline:
            v = _match_inline(inline, name)
            if v is not None:
                attrs = v.get("attributes") or {}
                attr_n, attr_v = (next(iter(attrs.items())) if attrs else ("", ""))
                out[name] = {
                    "found": True,
                    "in_stock": bool(v.get("is_in_stock")) and bool(v.get("is_purchasable", True)),
                    "price": _variation_price(v),
                    "variation_id": v.get("variation_id"),
                    "deep_link": _deep_link(url, attr_n, attr_v),
                }
                continue

        match = _lookup_option_value(attr_options, name)
        if match is None:
            log.warning("no dropdown option matched %r", name)
            out[name] = _not_found(url)
            continue
        attr_name, option_value = match
        deep = _deep_link(url, attr_name, option_value)
        try:
            data = _ajax_variation(session, base, product_id, attr_name, option_value, referer=url)
        except AjaxError as e:
            # Transient failure — flag as not-found so caller preserves last
            # known state instead of recording a false OOS (which would later
            # be misread as a restock when ajax recovers).
            log.warning("ajax transient error for %r: %s", name, e)
            out[name] = _not_found(deep)
            continue
        if data is None:
            # No matching variation → "Sorry, no products matched your selection"
            out[name] = {"found": True, "in_stock": False, "price": "", "variation_id": None, "deep_link": deep}
            continue
        out[name] = {
            "found": True,
            "in_stock": bool(data.get("is_in_stock")) and bool(data.get("is_purchasable", True)),
            "price": _variation_price(data),
            "variation_id": data.get("variation_id"),
            "deep_link": deep,
        }
    return out


def check(
    url: str, watch_variants: list[str], session: Optional[requests.Session] = None
) -> dict[str, dict]:
    """Return {variant_or_label: {"found", "in_stock", "price", "variation_id"}}.

    For simple products, `watch_variants[0]` is used as the result key (label
    for notifications). If no variants are provided, the URL is used.
    """
    session = session or _session()
    cached = _PAGE_CACHE.get(url)
    if cached is None:
        # `prefetch()` hat die Seite ggf. schon geholt; sonst ganz normal selbst.
        page = _HTML_CACHE.pop(url, None) or _fetch(url, session)
        soup = BeautifulSoup(page, "html.parser")
        is_simple = _is_simple_product(soup)
        form = None if is_simple else _parse_variable_form(soup)
        cached = (soup, is_simple, form)
        _PAGE_CACHE[url] = cached
    soup, is_simple, form = cached

    if is_simple:
        label = watch_variants[0] if watch_variants else url
        return {label: _check_simple_from_soup(soup, url)}

    return _check_variable(session, url, watch_variants, form)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: python -m src.bgpharma <product_url> [<variant_name> ...]")
        sys.exit(2)
    url = sys.argv[1].strip()
    variants = [v.strip() for v in sys.argv[2:]]
    result = check(url, variants)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def list_variants(url: str, session: Optional[requests.Session] = None) -> dict:
    """Was gibt es auf dieser Produktseite? → {"title", "simple", "variants"}.

    Für `/product add`: Der Discord-Worker kann das nicht selbst, er hat keinen
    Browser und kennt die Seite nicht. Also liest der Bot sie einmal ein und
    legt das Ergebnis ab, damit die Auswahl danach per Autocomplete geht.

    Bei einem einfachen Produkt (die URL IST das Produkt) bleibt `variants`
    leer — dort gibt es nichts auszuwählen, der Titel ist die ganze Wahrheit.
    Bei einem variablen Produkt kommen die Dropdown-Einträge zurück, und zwar
    im ORIGINALWORTLAUT: `check()` vergleicht später damit, ein geglätteter
    Text würde ins Leere greifen.
    """
    session = session or _session()
    soup = BeautifulSoup(_fetch(url, session), "html.parser")

    h1 = soup.find("h1")
    title = (h1.get_text() if h1 else "").strip() or url.rstrip("/").rsplit("/", 1)[-1]

    if _is_simple_product(soup):
        return {"title": title, "simple": True, "variants": []}

    form = soup.find("form", class_="variations_form")
    variants: list[str] = []
    if form:
        for sel in form.find_all("select"):
            for o in sel.find_all("option"):
                if not (o.get("value") or "").strip():
                    continue          # der "Bitte wählen"-Platzhalter
                label = (o.get_text() or "").strip()
                if label and label not in variants:
                    variants.append(label)

    return {"title": title, "simple": False, "variants": variants}
