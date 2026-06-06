"""Read the current price of a PlayStation Store game.

The PS Store is a JavaScript SPA, so there's no plain `p.price` element to scrape
like on the WooCommerce shop. The price *is* server-rendered though — it sits in
the page's embedded JSON. Two relevant blobs:

- `<script id="__NEXT_DATA__">` — the Next.js payload (product name etc.), but on
  product pages the price node is often left un-hydrated here.
- `<script id="env:...">` — one per on-page web component ("batarang"), each with
  its own normalized Apollo `cache`. The buyable price lives on the
  `GameCTA` node of type `ADD_TO_CART`, under `price`:

      {"__typename": "Price",
       "basePrice": "€59,99", "discountedPrice": "€29,99",
       "basePriceValue": 5999, "discountedValue": 2999,   # cents
       "displayDiscountText": "-50%", "currencyCode": "EUR", "isFree": false}

We parse every `env:*` cache, pick the ADD_TO_CART GameCTA that actually carries a
price, and read the numbers. `discountedValue` (cents) is the current price; that's
what the price-drop watcher compares against. Locale is forced via the `/de-de/`
URL, so EUR prices come back regardless of the runner's IP.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_ENV_SCRIPT = re.compile(
    r'<script id="env:[^"]+" type="application/json">(.*?)</script>', re.S
)


def _fetch(url: str) -> str:
    """GET the product page with retries for transient HTTP/network errors."""
    headers = {"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.7"}
    last_err: Optional[Exception] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            r = requests.get(url, headers=headers, timeout=20)
            if 500 <= r.status_code < 600 and attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            r.encoding = "utf-8"  # € arrives as UTF-8; requests guesses latin-1
            return r.text
        except requests.RequestException as e:
            last_err = e
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    if last_err:
        raise last_err
    raise RuntimeError(f"fetch {url} exhausted retries")


def _iter_env_caches(html: str):
    """Yield the Apollo `cache` dict from each `env:*` JSON script (skip bad ones)."""
    for m in _ENV_SCRIPT.finditer(html):
        try:
            env = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        cache = env.get("cache")
        if isinstance(cache, dict):
            yield cache


def _pick_price_node(html: str) -> Optional[dict]:
    """Find the buyable price across all env caches.

    Prefer the ADD_TO_CART GameCTA (the actual purchase price). Fall back to any
    GameCTA carrying a price (covers games only offered via subscription upsell),
    so we still surface *a* number instead of silently reporting not-found.
    """
    fallback: Optional[dict] = None
    for cache in _iter_env_caches(html):
        for node in cache.values():
            if not isinstance(node, dict) or node.get("__typename") != "GameCTA":
                continue
            price = node.get("price")
            if not isinstance(price, dict) or price.get("discountedValue") is None:
                continue
            if node.get("type") == "ADD_TO_CART":
                return price
            if fallback is None:
                fallback = price
    return fallback


def _product_name(html: str) -> str:
    """Best-effort product name from any env cache Product node, else the OG title."""
    for cache in _iter_env_caches(html):
        for key, node in cache.items():
            if key.startswith("Product:") and isinstance(node, dict) and node.get("name"):
                return str(node["name"])
    m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    return m.group(1).strip() if m else ""


def _cents(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def check(url: str, name: str = "") -> dict:
    """Return the current price snapshot for one PS Store game.

    Shape:
      {"found": bool,           # price successfully read
       "name": str,             # game title (config name wins, else scraped)
       "price": str,            # display string for the current price ("€29,99")
       "base_price": str,       # display string for the undiscounted price
       "price_value": int|None, # current price in cents — the watcher's signal
       "base_value": int|None,  # undiscounted price in cents
       "discount_text": str,    # e.g. "-50%" (empty when not on sale)
       "is_free": bool,
       "currency": str,
       "deep_link": str}        # always the product URL
    """
    html = _fetch(url)
    price = _pick_price_node(html)
    scraped_name = _product_name(html)
    if price is None:
        return {
            "found": False, "name": name or scraped_name, "price": "", "base_price": "",
            "price_value": None, "base_value": None, "discount_text": "",
            "is_free": False, "currency": "", "deep_link": url,
        }
    discounted = _cents(price.get("discountedValue"))
    base = _cents(price.get("basePriceValue"))
    return {
        "found": True,
        "name": name or scraped_name,
        "price": str(price.get("discountedPrice") or price.get("basePrice") or ""),
        "base_price": str(price.get("basePrice") or ""),
        "price_value": discounted,
        "base_value": base,
        "discount_text": str(price.get("displayDiscountText") or ""),
        "is_free": bool(price.get("isFree")),
        "currency": str(price.get("currencyCode") or ""),
        "deep_link": url,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: python -m src.playstation <product_url>")
        sys.exit(2)
    print(json.dumps(check(sys.argv[1].strip()), indent=2, ensure_ascii=False))
