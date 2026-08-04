"""Stock-Watcher für die BG-Shop-Produkte aus config.yml.

`check_products` scraped alle konfigurierten Produkte (via `bgpharma`),
führt den Bestands-/Preis-State in state.json und erkennt Transitions
(Restock, Out-of-stock). Enthält den Site-wide-Outage-Guard, der falsche
Massen-OOS-Reads (Katalog-Resync, Currency-Flip) unterdrückt.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import bgpharma
from .config import product_state_key, product_urls
from .pricing import price_value

log = logging.getLogger(__name__)


def _check_combined(urls: list[str], watch: list[str]) -> dict[str, dict]:
    """Check each URL and merge per variant — in-stock wins, else first found."""
    per_url: list[dict[str, dict]] = []
    for url in urls:
        try:
            per_url.append(bgpharma.check(url, watch))
        except Exception as e:
            log.error("check failed for %s: %s", url, e)
            per_url.append({})

    merged: dict[str, dict] = {}
    for variant in watch or [""]:
        in_stock_info = None
        fallback_info = None
        for current in per_url:
            info = current.get(variant)
            if not info:
                continue
            if info.get("in_stock"):
                in_stock_info = info
                break
            if info.get("found") and fallback_info is None:
                fallback_info = info
        if in_stock_info is not None:
            merged[variant] = {**in_stock_info, "found": True}
        elif fallback_info is not None:
            merged[variant] = {**fallback_info, "found": True, "in_stock": False}
        else:
            merged[variant] = {"found": False, "in_stock": False, "price": "", "deep_link": urls[0]}
    return merged


def check_products(cfg: dict, state: dict) -> tuple[list[dict], list[dict], list[dict]]:
    bgpharma.reset_page_cache()  # products sharing a URL fetch it once per run
    # Alle Seiten parallel vorladen, bevor die Schleife startet — sonst wartet
    # jedes Produkt einzeln auf den Shop. Doppelte URLs (alle Peptide teilen sich
    # eine Seite) filtert prefetch() selbst raus.
    bgpharma.prefetch([u for p in (cfg.get("products") or []) for u in product_urls(p)])
    products_state = state.setdefault("products", {})
    bot_stats = state.setdefault("bot_stats", {})
    run_iso = datetime.now(timezone.utc).isoformat()
    bot_stats.setdefault("first_check_at", run_iso)
    bot_stats["last_check_at"] = run_iso
    bot_stats["total_checks"] = bot_stats.get("total_checks", 0) + 1
    statuses: list[dict] = []
    restocks: list[dict] = []
    oos_alerts: list[dict] = []

    # Phase 1 — scrape every product up front. We need all reads in hand before
    # deciding transitions so we can recognize a site-wide outage (phase 2).
    scanned: list[dict] = []
    for product in cfg.get("products") or []:
        urls = product_urls(product)
        if not urls:
            continue
        url = urls[0]
        state_key = product_state_key(product, urls)
        watch = product.get("watch_variants") or []
        prev = products_state.setdefault(state_key, {})
        try:
            current = _check_combined(urls, watch) if len(urls) > 1 else bgpharma.check(url, watch)
        except Exception as e:
            log.error("check failed for %s: %s", url, e)
            current = None
        scanned.append({
            "url": url, "state_key": state_key, "name": product.get("name") or url,
            "watch": watch, "prev": prev, "current": current,
        })

    # Phase 2 — detect a suspected site-wide outage. If *every* variant that was
    # in stock last run now reads OOS (or failed to scrape) in the same run,
    # it's almost certainly a site-side glitch — a catalog re-sync or a
    # currency/region context flip that marks the whole shop non-purchasable —
    # not a real catalog-wide sellout. (Observed 2026-05-28: all 6 in-stock
    # variants flipped OOS at 11:30 UTC and back at 12:30 UTC, every price a few
    # cents higher: the signature of a currency refresh, not six real restocks.)
    # For those variants we hold last-known state instead of recording the OOS,
    # which also prevents the false restock ping storm when the site recovers.
    prev_up: set[tuple[str, str]] = set()
    confirmed_up_now: set[tuple[str, str]] = set()
    for item in scanned:
        cur = item["current"]
        variants = list(cur.keys()) if cur else (item["watch"] or ["(unknown)"])
        for variant in variants:
            key = (item["state_key"], variant)
            if item["prev"].get(variant, {}).get("in_stock"):
                prev_up.add(key)
            info = cur.get(variant) if cur else None
            if info and info.get("found") and info.get("in_stock"):
                confirmed_up_now.add(key)
    site_wide_outage = len(prev_up) >= 3 and prev_up.isdisjoint(confirmed_up_now)
    if site_wide_outage:
        log.warning(
            "suspected site-wide outage: all %d previously in-stock variant(s) "
            "read OOS/failed this run — holding last-known state, suppressing alerts",
            len(prev_up),
        )

    # Phase 3 — apply per-variant transitions.
    for item in scanned:
        url, name = item["url"], item["name"]
        watch, prev, current = item["watch"], item["prev"], item["current"]

        if current is None:
            # Whole-product scrape failed — surface last known state so the
            # dashboard doesn't black out during a transient site outage.
            for variant in watch or ["(unknown)"]:
                prev_variant = prev.get(variant, {})
                statuses.append({
                    "product_name": name, "product_url": url, "variant": variant,
                    "in_stock": bool(prev_variant.get("in_stock")),
                    "price": prev_variant.get("price", ""),
                    "previous_price": prev_variant.get("previous_price", ""),
                    "out_since": prev_variant.get("out_since", ""),
                    "found": False,
                    "deep_link": url, "error": True,
                })
            continue

        for variant, info in current.items():
            entry = prev.setdefault(variant, {})
            deep = info.get("deep_link") or url

            # Variant lookup failed (missing variations_form, dropdown option
            # gone, ajax transient error). Treat as "unknown" — preserve last
            # known state so a partial site outage doesn't flip everything to
            # OOS and then fire a false restock on the next successful check.
            if not info.get("found"):
                statuses.append({
                    "product_name": name, "product_url": url, "variant": variant,
                    "in_stock": bool(entry.get("in_stock")),
                    "price": entry.get("price", ""),
                    "previous_price": entry.get("previous_price", ""),
                    "out_since": entry.get("out_since", ""),
                    "found": False,
                    "deep_link": deep,
                    "error": True,
                })
                continue

            in_stock_now = bool(info["in_stock"])
            in_stock_prev = entry.get("in_stock")

            # Site-wide-outage guard: a previously in-stock variant reading OOS
            # during a mass flip is an unreliable read. Preserve last-known
            # state (don't stamp out_since, don't flip in_stock, don't alert) so
            # the recovery doesn't fire a false restock ping. Flagged uncertain
            # on the dashboard via `error`.
            if site_wide_outage and in_stock_prev and not in_stock_now:
                statuses.append({
                    "product_name": name, "product_url": url, "variant": variant,
                    "in_stock": True,
                    "price": entry.get("price", ""),
                    "previous_price": entry.get("previous_price", ""),
                    "out_since": entry.get("out_since", ""),
                    "found": True,
                    "deep_link": deep,
                    "error": True,
                })
                continue

            new_price = info.get("price", "")
            prev_price = entry.get("price", "")
            now_iso = datetime.now(timezone.utc).isoformat()
            out_since_before = entry.get("out_since")

            # OOS bookkeeping: stamp out_since when going (or staying) out of stock.
            if not in_stock_now and not entry.get("out_since"):
                entry["out_since"] = now_iso
            elif in_stock_now:
                entry.pop("out_since", None)

            # Price-change bookkeeping: remember the last different price.
            if in_stock_now and new_price and prev_price and new_price != prev_price:
                entry["previous_price"] = prev_price

            # Low/high tracking + price history (numeric, deduped, last 30).
            new_val = price_value(new_price)
            if new_val is not None:
                low_val = price_value(entry.get("lowest_price", ""))
                if low_val is None or new_val < low_val:
                    entry["lowest_price"] = new_price
                    entry["lowest_price_at"] = now_iso
                high_val = price_value(entry.get("highest_price", ""))
                if high_val is None or new_val > high_val:
                    entry["highest_price"] = new_price
                    entry["highest_price_at"] = now_iso
                history = entry.setdefault("price_history", [])
                if not history or history[-1] != new_val:
                    history.append(new_val)
                    if len(history) > 30:
                        entry["price_history"] = history[-30:]

            statuses.append({
                "product_name": name,
                "product_url": url,
                "variant": variant,
                "in_stock": in_stock_now,
                "price": new_price or entry.get("price", ""),
                "previous_price": entry.get("previous_price", ""),
                "out_since": entry.get("out_since", ""),
                "found": True,
                "deep_link": deep,
                "error": False,
            })

            if in_stock_prev is None:
                log.info("[%s] %s: baseline %s", name, variant, "IN STOCK" if in_stock_now else "out")
            elif in_stock_now and not in_stock_prev:
                log.info("notify restock: %s", variant)
                restocks.append({
                    "product_name": name, "product_url": url, "deep_link": deep,
                    "variant": variant, "price": new_price,
                })
                entry["restock_count"] = entry.get("restock_count", 0) + 1
                entry["last_restock_at"] = now_iso
                bot_stats["total_restocks"] = bot_stats.get("total_restocks", 0) + 1
                if out_since_before:
                    periods = entry.setdefault("oos_periods", [])
                    periods.append({"start": out_since_before, "end": now_iso})
                    if len(periods) > 20:
                        entry["oos_periods"] = periods[-20:]
            elif not in_stock_now and in_stock_prev:
                log.info("notify out-of-stock: %s", variant)
                oos_alerts.append({
                    "product_name": name, "product_url": url, "deep_link": deep,
                    "variant": variant, "last_price": prev_price,
                })

            entry["in_stock"] = in_stock_now
            # Only persist the displayed price when in stock — OOS variation
            # pages still expose a price, but it's not the actionable
            # "next-purchase" price and would pollute the `_last XX_` hint.
            if in_stock_now and new_price:
                entry["price"] = new_price
            entry["found"] = True

    # Drop state for products no longer in config so state.json doesn't grow
    # forever. Top-level keys (message ids, bot_stats, last_deploy_sha) stay.
    expected_keys = {
        product_state_key(p, product_urls(p))
        for p in cfg.get("products") or []
        if product_urls(p)
    }
    for key in list(products_state.keys()):
        if key not in expected_keys:
            log.info("removing stale state entry: %s", key)
            del products_state[key]

    return statuses, restocks, oos_alerts
