"""Orchestrator: check products, update Discord dashboard, alert on restocks.

- Dashboard: persistent embed, silently edited in place every run. Never spams.
- Restock alert: one fresh embed message per restocked variant, with @-mentions.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml

from . import bgpharma, forum, notify, orders

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yml"
STATE_PATH = ROOT / "state.json"

COLOR_IN_STOCK = 0x57F287    # Discord native green
COLOR_OUT      = 0x95A5A6    # Discord native gray
COLOR_WARN     = 0xFEE75C    # Discord native yellow


def fetch_usd_eur_rate() -> Optional[float]:
    """Daily USD->EUR rate from Frankfurter (ECB-backed, no key). None on error."""
    try:
        r = requests.get("https://api.frankfurter.app/latest?from=USD&to=EUR", timeout=10)
        r.raise_for_status()
        return float(r.json()["rates"]["EUR"])
    except Exception as e:
        log.warning("USD/EUR rate fetch failed: %s", e)
        return None


_USD_PATTERN = re.compile(r"\$\s*([\d,]+\.?\d*)")
_PRICE_VALUE_PATTERN = re.compile(r"([\d,]+\.?\d*)")


def _price_value(raw: str) -> Optional[float]:
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


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("state.json unreadable, starting fresh: %s", e)
        return {}


def load_ping_user_ids(cfg: dict) -> list[str]:
    """Resolve Discord user IDs from env (PING_USER_IDS, comma/semicolon/space
    separated), else fall back to `notifications.ping_user_ids` in config.

    Env-first keeps personal Discord IDs out of the public repo while still
    allowing local dev to use config.yml.
    """
    env = os.environ.get("PING_USER_IDS", "").strip()
    if env:
        return [u for u in re.split(r"[,;\s]+", env) if u]
    notif = cfg.get("notifications") or {}
    return [str(u) for u in (notif.get("ping_user_ids") or [])]


def save_state(state: dict) -> None:
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def _product_urls(product: dict) -> list[str]:
    """Normalize `url` (str) and/or `urls` (list) into a flat list of URLs."""
    urls: list[str] = []
    single = product.get("url")
    if single:
        urls.append(single)
    for u in product.get("urls") or []:
        if u and u not in urls:
            urls.append(u)
    return urls


def _product_state_key(product: dict, urls: list[str]) -> str:
    """Synthetic key for combined products so per-URL data doesn't collide."""
    if len(urls) > 1:
        return f"combined:{product.get('name') or urls[0]}"
    return urls[0]


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
        urls = _product_urls(product)
        if not urls:
            continue
        url = urls[0]
        state_key = _product_state_key(product, urls)
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
            new_val = _price_value(new_price)
            if new_val is not None:
                low_val = _price_value(entry.get("lowest_price", ""))
                if low_val is None or new_val < low_val:
                    entry["lowest_price"] = new_price
                    entry["lowest_price_at"] = now_iso
                high_val = _price_value(entry.get("highest_price", ""))
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
            # "next-purchase" price and would pollute the `_war XX_` hint.
            if in_stock_now and new_price:
                entry["price"] = new_price
            entry["found"] = True

    # Drop state for products no longer in config so state.json doesn't grow
    # forever. Top-level keys (message ids, bot_stats, last_deploy_sha) stay.
    expected_keys = {
        _product_state_key(p, _product_urls(p))
        for p in cfg.get("products") or []
        if _product_urls(p)
    }
    for key in list(products_state.keys()):
        if key not in expected_keys:
            log.info("removing stale state entry: %s", key)
            del products_state[key]

    return statuses, restocks, oos_alerts


def _days_since(iso: str) -> int:
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return -1
    return max(0, (datetime.now(timezone.utc) - when).days)


def _oos_label(iso: str) -> str:
    """OOS text (no leading separator). Used inside the └-line under each variant."""
    if not iso:
        return "OOS"
    days = _days_since(iso)
    if days < 0:
        return "OOS"
    if days == 0:
        return "OOS seit heute"
    if days == 1:
        return "OOS seit 1 Tag"
    return f"OOS seit {days} Tagen"


def _price_change_suffix(prev_price: str, current_price: str) -> str:
    if not prev_price or not current_price or prev_price == current_price:
        return ""
    return f"⠀·⠀_war {prev_price}_"


def _humanize_duration(seconds: float) -> str:
    """Compact human-readable duration: '12 s', '4 min', '3 h', '2 T 14 h', '3 Wo'."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h"
    days = hours // 24
    if days < 14:
        hours_rem = hours % 24
        if hours_rem and days < 7:
            return f"{days} T {hours_rem} h"
        return f"{days} T"
    return f"{days // 7} Wo"


def _humanize_ago(iso: str) -> str:
    if not iso:
        return ""
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return ""
    delta = (datetime.now(timezone.utc) - when).total_seconds()
    if delta < 60:
        return "gerade eben"
    return f"vor {_humanize_duration(delta)}"


# OOS-Phasen kürzer als das sind fast immer Scraper-/Outage-Artefakte: ein
# site-weiter Hänger liest ~1 Run lang OOS und „erholt" sich danach sofort
# wieder. Der Outage-Guard (siehe check_products) fängt nur site-weite
# Ausfälle ab — ein einzelner Hänger bei genau einem Produkt rutscht durch
# und würde sonst als 1h-Phantom-OOS den Schnitt verfälschen.
MIN_OOS_PERIOD_SECONDS = 90 * 60


def _median(values: list) -> float:
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def _oos_period_durations(periods: list) -> list:
    """Echte OOS-Dauern in Sekunden — Artefakt-Blips rausgefiltert."""
    out = []
    for p in periods or []:
        try:
            start = datetime.fromisoformat(p["start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(p["end"].replace("Z", "+00:00"))
        except (ValueError, KeyError, TypeError):
            continue
        secs = (end - start).total_seconds()
        if secs >= MIN_OOS_PERIOD_SECONDS:
            out.append(secs)
    return out


def _typical_oos_duration(periods: list) -> tuple:
    """Median der echten OOS-Phasen + deren Anzahl. Median statt Mittelwert,
    damit ein einzelner Ausreißer (oder ein durchgerutschtes Artefakt) die
    Zahl nicht kippt. Gibt ("—", 0) zurück, wenn keine echte Phase übrig ist."""
    durations = _oos_period_durations(periods)
    if not durations:
        return "—", 0
    return _humanize_duration(_median(durations)), len(durations)


_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list) -> str:
    if not values or len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return _SPARK_CHARS[3] * len(values)
    span = hi - lo
    step = len(_SPARK_CHARS) - 1
    return "".join(_SPARK_CHARS[min(int((v - lo) / span * step), step)] for v in values)


def _fmt_price_value(value: float, sample: str, rate: Optional[float]) -> str:
    """Format a numeric price using the currency style of `sample` (USD→EUR via rate)."""
    if "$" in (sample or ""):
        return display_price(f"${value:.2f}", rate)
    if "€" in (sample or ""):
        return f"€{value:.2f}"
    return f"{value:.2f}"


def _has_last_known_state(s: dict) -> bool:
    return bool(s.get("price")) or bool(s.get("in_stock")) or bool(s.get("out_since"))


def _dashboard_sort_key(s: dict) -> int:
    if s.get("error") and not _has_last_known_state(s):
        return 2
    if not s.get("found") and not s.get("error"):
        return 2
    return 0 if s["in_stock"] else 1


def _variant_labels(cfg: dict) -> dict:
    """Optionale Anzeige-Aliase pro Variante: {match_string: label}.

    Betrifft NUR die Darstellung (Dashboard, Stats, Alerts) — das Matching
    gegen die Website und der state.json-Key bleiben der rohe watch_variants-
    String. Nötig für variable Produkte (z.B. Modafinil), wo der watch_variant
    exakt das Website-Variantenlabel treffen muss; bei simple products kann man
    den watch_variant direkt umbenennen (er ist dort nur ein Label)."""
    out: dict = {}
    for p in cfg.get("products") or []:
        for match, label in (p.get("variant_labels") or {}).items():
            out[str(match)] = str(label)
    return out


def build_dashboard_embed(
    statuses: list[dict], usd_eur: Optional[float] = None, labels: Optional[dict] = None
) -> dict:
    labels = labels or {}
    blocks: list[str] = []
    for s in sorted(statuses, key=_dashboard_sort_key):
        link = s.get("deep_link") or s.get("product_url", "")
        klick = f"⠀·⠀[Klick]({link})" if link else ""
        uncertain = s.get("error") and _has_last_known_state(s)

        if s.get("error") and not _has_last_known_state(s):
            sub = f"⚠️⠀check failed"
        elif not s["found"] and not s.get("error"):
            sub = f"⚠️⠀nicht gefunden"
        elif s["in_stock"]:
            shown_price = display_price(s.get("price", ""), usd_eur)
            shown_prev = display_price(s.get("previous_price", ""), usd_eur)
            price = f"⠀·⠀**{shown_price}**" if shown_price else ""
            delta = _price_change_suffix(shown_prev, shown_price)
            warn = "⠀·⠀⚠️_check unsicher_" if uncertain else ""
            sub = f"🟢⠀in stock{price}{delta}{warn}"
        else:
            shown_last = display_price(s.get("price", ""), usd_eur)
            last_suffix = f"⠀·⠀_zuletzt {shown_last}_" if shown_last else ""
            warn = "⠀·⠀⚠️_check unsicher_" if uncertain else ""
            sub = f"🔴⠀out of stock{last_suffix}{warn}"

        disp = labels.get(s["variant"], s["variant"])
        blocks.append(f"**{disp}**\n└⠀{sub}{klick}")

    any_in_stock = any(s["in_stock"] for s in statuses if s.get("found") or _has_last_known_state(s))
    any_hard_error = any(s.get("error") and not _has_last_known_state(s) for s in statuses)
    color = COLOR_WARN if any_hard_error and not any_in_stock else (COLOR_IN_STOCK if any_in_stock else COLOR_OUT)

    return {
        "author": {"name": "bgpharmadrugs.to", "url": "https://bgpharmadrugs.to/"},
        "title": "BG Pharma · Status",
        "color": color,
        "description": "\n\n".join(blocks) if blocks else "_keine Produkte konfiguriert_",
        "footer": {"text": "Letzter Check"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_stats_embed(cfg: dict, state: dict, usd_eur: Optional[float] = None) -> dict:
    """Persistent stats card — edited in place each run. Pin manually once."""
    bot_stats = state.get("bot_stats", {})
    products_state = state.get("products", {})
    labels = _variant_labels(cfg)

    checks = bot_stats.get("total_checks", 0)
    restocks = bot_stats.get("total_restocks", 0)
    bot_sub = [
        f"Checks gesamt: **{checks:,}**".replace(",", " "),
        f"Restocks erkannt: **{restocks}**",
    ]
    first_iso = bot_stats.get("first_check_at", "")
    if first_iso:
        try:
            when = datetime.fromisoformat(first_iso.replace("Z", "+00:00"))
            delta = (datetime.now(timezone.utc) - when).total_seconds()
            duration = _humanize_duration(delta) if delta >= 60 else "<1 min"
            bot_sub.append(f"Aktiv seit: **{duration}**")
        except (ValueError, TypeError):
            pass
    bot_lines = ["🤖⠀**Bot**"]
    for i, line in enumerate(bot_sub):
        prefix = "└⠀" if i == len(bot_sub) - 1 else "├⠀"
        bot_lines.append(prefix + line)
    blocks = ["\n".join(bot_lines)]

    entries: list[tuple[str, str, dict]] = []
    for product in cfg.get("products") or []:
        urls = _product_urls(product)
        if not urls:
            continue
        watch = product.get("watch_variants") or []
        emoji = product.get("emoji") or "💊"
        product_data = products_state.get(_product_state_key(product, urls), {})
        for variant in watch:
            e = product_data.get(variant)
            if not e:
                continue
            entries.append((emoji, variant, e))

    entries.sort(key=lambda t: 0 if t[2].get("in_stock") else 1)

    for emoji, variant, e in entries:
        sample_price = e.get("price", "") or e.get("lowest_price", "")
        lines = [f"{emoji}⠀**{labels.get(variant, variant)}**"]

        history = e.get("price_history", [])
        spark = _sparkline(history)
        if spark:
            first_str = _fmt_price_value(history[0], sample_price, usd_eur)
            last_str = _fmt_price_value(history[-1], sample_price, usd_eur)
            trend = f"**{first_str} → {last_str}**" if first_str != last_str else f"**{first_str}**"
            lines.append(f"├⠀📈⠀`{spark}`⠀{trend}")
        elif history:
            only_str = _fmt_price_value(history[0], sample_price, usd_eur)
            lines.append(f"├⠀📈⠀**{only_str}**")

        oos_typical, oos_n = _typical_oos_duration(e.get("oos_periods", []))
        if oos_n:
            lines.append(f"├⠀⏱⠀**OOS-Dauer Ø: {oos_typical}**⠀·⠀{oos_n}×")
        else:
            lines.append("├⠀⏱⠀OOS-Dauer Ø: —")

        low = display_price(e.get("lowest_price", ""), usd_eur)
        high = display_price(e.get("highest_price", ""), usd_eur)
        low_ago = _humanize_ago(e.get("lowest_price_at", ""))
        high_ago = _humanize_ago(e.get("highest_price_at", ""))
        if low:
            lines.append(f"├⠀tief {low}" + (f" ({low_ago})" if low_ago else ""))
        if high:
            lines.append(f"├⠀hoch {high}" + (f" ({high_ago})" if high_ago else ""))

        rc = e.get("restock_count", 0)
        last_restock = _humanize_ago(e.get("last_restock_at", ""))
        if rc:
            rline = f"└⠀🔄⠀**{rc} Restocks**"
            if last_restock:
                rline += f"⠀·⠀letzter {last_restock}"
            lines.append(rline)
        else:
            lines.append("└⠀🔄⠀noch keine Restocks erkannt")

        blocks.append("\n".join(lines))

    return {
        "author": {"name": "✦⠀⠀bgnotify · Stats⠀⠀✦"},
        "color": 0x5865F2,
        "description": "\n\n".join(blocks),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _git(*args: str) -> str:
    """Run `git <args>` in the repo root. Empty string on any failure."""
    try:
        out = subprocess.check_output(
            ["git", *args], cwd=str(ROOT), text=True, stderr=subprocess.DEVNULL,
        )
        return out.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return ""


def _sha_reachable(sha: str) -> bool:
    """True if `sha` exists in local git history. Stale SHAs (rebase/force-push)
    return False so the caller can fall back to a HEAD-only deploy embed instead
    of silently dropping the announcement."""
    if not sha:
        return False
    try:
        subprocess.check_output(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=str(ROOT), stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False


def _commits_since(prev_sha: str) -> list[tuple[str, str]]:
    """Return [(short_sha, subject), ...] for commits in prev_sha..HEAD.

    Excludes the bot's own `update state` commits so the deploy feed only
    shows meaningful code/config changes.
    """
    if not prev_sha:
        return []
    raw = _git("log", f"{prev_sha}..HEAD", "--pretty=format:%h|%s")
    if not raw:
        return []
    out: list[tuple[str, str]] = []
    for line in raw.splitlines():
        if "|" not in line:
            continue
        sha, subject = line.split("|", 1)
        if subject.strip().lower().startswith("update state"):
            continue
        out.append((sha, subject))
    return out


def _head_commit_subject(head_sha: str) -> str:
    raw = _git("log", "-1", "--pretty=format:%s", head_sha)
    return raw.strip()


def build_updates_embed(commits: list[tuple[str, str]], head_sha: str) -> dict:
    if commits:
        lines = [f"`{sha}`⠀{subject}" for sha, subject in commits[:15]]
        if len(commits) > 15:
            lines.append(f"_…und {len(commits) - 15} weitere_")
        description = "\n".join(lines)
    else:
        # No reachable commits in prev..HEAD (rebase/force-push or first
        # post-feature run). Show at least the head subject so the channel
        # still surfaces that something shipped.
        subject = _head_commit_subject(head_sha)
        if subject:
            description = f"`{head_sha[:7]}`⠀{subject}"
        else:
            description = f"`{head_sha[:7]}`"
    return {
        "author": {"name": "✦⠀⠀Deploy⠀⠀✦"},
        "title": f"Update · `{head_sha[:7]}`",
        "description": description,
        "color": 0x5865F2,
        "footer": {"text": "bgnotify"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


_ISO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def build_forum_embed(post: dict) -> dict:
    """Embed for one new BG forum post — green accent, minimal chrome."""
    excerpt = post.get("excerpt") or ""
    if len(excerpt) > 600:
        excerpt = excerpt[:597].rstrip() + "…"
    embed = {
        "author": {"name": "✦ ⠀ neuer Post⠀⠀✦"},
        "title": post.get("thread_title") or "(ohne Titel)",
        "url": post.get("url", ""),
        "description": excerpt or "_(kein Snippet)_",
        "color": COLOR_IN_STOCK,
        "footer": {"text": "thinksteroids.com"},
    }
    posted_at = post.get("posted_at") or ""
    if _ISO_PREFIX.match(posted_at):
        embed["timestamp"] = posted_at
    return embed


def check_forum(cfg: dict, state: dict) -> list[dict]:
    """Return new posts (oldest first) and update state. Seeds silently on first run.

    State shape:
      forum.seen_post_ids: list[str]  (last ~200 post ids we've ever seen)
      forum.seeded: bool              (true once we've recorded a baseline)
      forum.last_check_at: iso str    (rate-limit gate)
    """
    forum_cfg = cfg.get("forum") or {}
    url = forum_cfg.get("search_url")
    if not url:
        return []
    expected_author = forum_cfg.get("author") or ""
    max_per_run = int(forum_cfg.get("max_per_run", 5))
    interval_min = int(forum_cfg.get("check_interval_minutes", 120))

    fstate = state.setdefault("forum", {})

    # Rate-limit gate. Be polite to Incapsula — only one Playwright run per
    # `interval_min` regardless of how often the bot's main cron fires.
    last_iso = fstate.get("last_check_at", "")
    if last_iso:
        try:
            last = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            if elapsed < interval_min * 60:
                log.info("forum: %.0f min since last check (< %d min gate) — skipping",
                         elapsed / 60, interval_min)
                return []
        except (ValueError, TypeError):
            pass

    # Stamp BEFORE fetch — if Playwright crashes or Incapsula blocks us, we
    # still respect the interval. Otherwise a hard failure would retry every
    # cron run, which is exactly the hammering pattern that gets us flagged.
    fstate["last_check_at"] = datetime.now(timezone.utc).isoformat()

    try:
        posts = forum.fetch_posts(url, expected_author=expected_author)
    except Exception as e:
        log.warning("forum fetch failed: %s", e)
        return []

    seen_ids = set(fstate.get("seen_post_ids") or [])
    seeded = bool(fstate.get("seeded"))

    # `posts` is newest-first from the scraper.
    new_posts = [p for p in posts if p["post_id"] not in seen_ids]

    # Persist seen ids (cap at 200 so state.json doesn't grow forever).
    merged = list(fstate.get("seen_post_ids") or [])
    for p in new_posts:
        if p["post_id"] not in seen_ids:
            merged.append(p["post_id"])
    fstate["seen_post_ids"] = merged[-200:]

    if not seeded:
        # First run after the feature lands — record current posts as the
        # baseline but don't notify (would dump the entire backlog at once).
        fstate["seeded"] = True
        log.info("forum: seeded with %d existing posts (no Discord notify)", len(posts))
        return []

    # Oldest first so Discord messages read chronologically. If too many
    # accumulated (long outage), keep the most-recent slice.
    new_posts.reverse()
    if len(new_posts) > max_per_run:
        new_posts = new_posts[-max_per_run:]
    return new_posts


def build_oos_embed(item: dict, usd_eur: Optional[float] = None, labels: Optional[dict] = None) -> dict:
    """Embed for an in-stock -> out-of-stock transition. Quieter than the
    restock alert — gray accent, last seen price, no order button, no pings."""
    last = display_price(item.get("last_price", ""), usd_eur)
    last_line = f"_zuletzt {last}_\n\n" if last else ""
    link = item.get("deep_link") or item.get("product_url") or ""
    link_line = f"[→⠀⠀Produktseite]({link})" if link else ""
    return {
        "author": {"name": "✦⠀⠀OUT OF STOCK⠀⠀✦"},
        "title": (labels or {}).get(item["variant"], item["variant"]),
        "description": last_line + link_line,
        "color": COLOR_OUT,
        "footer": {"text": "bgpharmadrugs.to"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_restock_embed(restock: dict, usd_eur: Optional[float] = None, labels: Optional[dict] = None) -> dict:
    shown = display_price(restock.get("price", ""), usd_eur)
    price_line = f"### ⠀{shown}\n\n" if shown else ""
    return {
        "author": {"name": "✦⠀⠀RESTOCKED⠀⠀✦"},
        "title": (labels or {}).get(restock["variant"], restock["variant"]),
        "description": (
            price_line
            + f"**[→⠀⠀Jetzt bestellen]({restock['deep_link']})**"
        ),
        "color": COLOR_IN_STOCK,
        "footer": {"text": "bgpharmadrugs.to"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def announce_deploy(state: dict, updates_webhook: str) -> None:
    """Post a deploy embed to the updates channel when HEAD has moved.

    Tracks the last-announced commit in `state["last_deploy_sha"]`. On first
    run after the feature lands, we record HEAD without posting (no history
    to diff against — would otherwise spam every old commit at once).
    """
    head_sha = _git("rev-parse", "HEAD")
    if not head_sha:
        return
    last_sha = state.get("last_deploy_sha", "")
    if head_sha == last_sha:
        return

    if last_sha and updates_webhook:
        reachable = _sha_reachable(last_sha)
        commits = _commits_since(last_sha) if reachable else []
        # When the previous SHA *is* reachable but the only commits in the
        # range are the bot's own "update state" entries (which _commits_since
        # filters out), this isn't a real deploy — advance last_sha silently
        # so we don't keep firing on every bot tick. The HEAD-only fallback
        # is only correct when the SHA is unreachable (rebase/force-push).
        if reachable and not commits:
            state["last_deploy_sha"] = head_sha
            return
        embed = build_updates_embed(commits, head_sha)
        ok = notify.send_update_announcement(updates_webhook, embed)
        if not ok:
            log.warning("deploy announcement failed — will retry next run")
            return  # keep last_sha so we re-try on the next run

    state["last_deploy_sha"] = head_sha


# --------------------------------------------------------------------------
# Bestellstatus (BG-Kundenkonto → privater Discord-Channel)
# --------------------------------------------------------------------------
_ORDER_STATUS_EMOJI = {
    "pending": "🕓", "processing": "📦", "preparing": "📦", "on-hold": "⏸️",
    "completed": "✅", "cancelled": "❌", "refunded": "↩️", "failed": "⚠️",
}
# Farbreise entlang des Ablaufs: grau → blau → türkis(Tracking) → grün.
# Seiten-/Negativzustände mit klar abgegrenzten Warnfarben.
_ORDER_STATUS_COLOR = {
    "pending":    0x95A5A6,  # grau      — wartet auf Zahlung (Start)
    "processing": 0x3498DB,  # blau      — in Bearbeitung (BG: "Preparing")
    "preparing":  0x3498DB,  # blau
    "on-hold":    0xE67E22,  # orange    — hängt / Klärung
    "completed":  0x57F287,  # grün      — fertig / angekommen (Ziel)
    "cancelled":  0xED4245,  # rot       — storniert
    "failed":     0x992D22,  # dunkelrot — fehlgeschlagen
    "refunded":   0x9B59B6,  # lila      — erstattet (Geld zurück)
}
# Tracking/„unterwegs" — türkis, sitzt visuell zwischen blau (Bearbeitung) und
# grün (angekommen). Bewusst NICHT grün, damit es sich von Completed abhebt.
_ORDER_TRACKING_COLOR = 0x1ABC9C
# Status, in denen eine Tracking-Note auftauchen kann.
_TRACKABLE_STATUS = {"processing", "preparing", "on-hold", "completed"}
# Endzustände — eine Bestellung in einem dieser Status gilt als "nicht offen".
_TERMINAL_STATUS = {"completed", "cancelled", "refunded", "failed"}


def _items_block(items: Optional[list[str]]) -> str:
    """Artikelzeilen für die Embed-Beschreibung (leer wenn keine bekannt)."""
    if not items:
        return ""
    return "\n\n" + "\n".join(f"·⠀{it}" for it in items)


def _order_link(url: Optional[str]) -> str:
    """Klickbarer Link zur Bestellseite (leer wenn keine URL)."""
    return f"\n\n[→⠀Bestellung ansehen]({url})" if url else ""


def build_order_status_embed(order: dict, fresh: bool = False, items: Optional[list[str]] = None) -> dict:
    slug = order.get("status", "")
    emoji = _ORDER_STATUS_EMOJI.get(slug, "📦")
    label = order.get("status_text") or slug or "—"
    head = "🆕⠀Neue Bestellung" if fresh else "Status-Update"
    return {
        "author": {"name": "✦⠀⠀Bestellung⠀⠀✦"},
        "title": f"#{order.get('order_id', '')}",
        "description": f"{head}\n{emoji}⠀**{label}**" + _items_block(items) + _order_link(order.get("url")),
        "color": _ORDER_STATUS_COLOR.get(slug, COLOR_WARN),
        "footer": {"text": "bgpharmadrugs.to"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_order_tracking_embed(order_id: str, links: list[str], items: Optional[list[str]] = None,
                               url: Optional[str] = None) -> dict:
    body = "\n".join(f"[→⠀Sendung verfolgen]({l})" for l in links)
    return {
        "author": {"name": "✦⠀⠀Tracking⠀⠀✦"},
        "title": f"#{order_id}",
        "description": f"🚚⠀**Tracking ist da**\n{body}" + _items_block(items) + _order_link(url) + "\n\n_Details in deiner Mail_",
        "color": _ORDER_TRACKING_COLOR,
        "footer": {"text": "via Hermes"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _parse_ids(raw: str) -> list[str]:
    return [x for x in re.split(r"[,;\s]+", raw or "") if x]


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("on", "an", "true", "yes", "y", "ja", "1")


def _order_enabled(st: dict, name: str) -> bool:
    """An/Aus-Schalter aus dem Gist-Feld `enabled` — pro Konto:

    - Dict (empfohlen): `{"a": "on", "b": "off"}`  → einfach on/off pro Konto
    - `true`                                        → alle Konten an
    - Liste `["a"]`                                 → nur diese Konten an
    - fehlt / `false`                               → aus (kein Login)

    So aktivierst du Tracking nur wenn du wirklich bestellt hast (Gist editieren,
    kein Code-Commit) — in Bestellpausen also NULL authentifizierte Logins.
    """
    en = st.get("enabled", False)
    if isinstance(en, dict):
        return _truthy(en.get(name, False))
    if en is True:
        return True
    if isinstance(en, list):
        return name in en
    return False


def _orders_due(acct: dict, interval_minutes: int, idle_interval_minutes: int) -> bool:
    """Ist dieser Account fällig? Zwei-Gang-Takt (offen→schnell, sonst Ruhe) mit
    ±10% Jitter. Kein last_check_at = noch nie geprüft = fällig."""
    has_open = any(v.get("status") not in _TERMINAL_STATUS for v in (acct.get("orders") or {}).values())
    effective = interval_minutes if has_open else idle_interval_minutes
    last = acct.get("last_check_at", "")
    if not last:
        return True
    try:
        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return True
    threshold = effective * 60 * random.uniform(0.9, 1.1)
    return (datetime.now(timezone.utc) - dt).total_seconds() >= threshold


def _check_one_account(name: str, webhook: str, user: str, pw: str,
                       ping_ids: list[str], role_ids: list[str], acct: dict) -> None:
    """Login + Diff + Post für GENAU einen Account; mutiert `acct` in place.

    last_check_at wird VOR dem Login gesetzt → Fehler-Drossel (ein gescheiterter
    Login löst trotzdem den Backoff aus, kein Hämmern). fetch() darf werfen —
    der Aufrufer fängt das und speichert den Stand (mit gesetztem last_check_at).
    """
    order_map = acct.setdefault("orders", {})
    initialized = acct.get("_initialized", False)
    acct["last_check_at"] = datetime.now(timezone.utc).isoformat()

    def want_detail(o: dict) -> bool:
        if not initialized:
            return False  # Baseline-Lauf: keine History nachladen
        prev = order_map.get(o["order_id"])
        if prev is None:
            return True  # neue Bestellung → Detailseite für die Artikel (+ ggf. Tracking)
        if o["status"] not in _TRACKABLE_STATUS:
            return False
        return not prev.get("tracking_posted")

    olist, details, cookies = orders.fetch(user, pw, want_detail, cookies=acct.get("cookies"))
    acct["cookies"] = cookies  # Session für nächsten Lauf merken (privates Gist)

    if not initialized:
        for o in olist:
            order_map[o["order_id"]] = {"status": o["status"], "tracking_posted": o["status"] == "completed"}
        acct["_initialized"] = True
        log.info("orders[%s]: Baseline gesetzt (%d Bestellungen), nichts gepostet", name, len(olist))
        return

    for o in olist:
        oid, slug = o["order_id"], o["status"]
        detail = orders.parse_order_detail(details[oid]) if oid in details else None
        items = (detail or {}).get("items") or None

        prev = order_map.get(oid)
        if prev is None:
            order_map[oid] = {"status": slug, "tracking_posted": False}
            prev = order_map[oid]
            if items:
                prev["items"] = items
            notify.send_order_update(webhook, build_order_status_embed(o, fresh=True, items=prev.get("items")), ping_ids, role_ids)
        else:
            if items and not prev.get("items"):
                prev["items"] = items  # Artikel einmalig sichern
            if prev.get("status") != slug:
                notify.send_order_update(webhook, build_order_status_embed(o, items=prev.get("items")), ping_ids, role_ids)
                prev["status"] = slug

        if not prev.get("tracking_posted") and detail and detail.get("tracking"):
            notify.send_order_update(webhook, build_order_tracking_embed(oid, detail["tracking"], items=prev.get("items"), url=o.get("url")), ping_ids, role_ids)
            prev["tracking_posted"] = True


def check_orders(cfg: dict, default_webhook: str, default_ping_ids: list[str], role_ids: list[str]) -> None:
    """BG-Bestellungen aller konfigurierten Accounts → privater Discord-Channel.

    Stand pro Account im privaten Gist unter `accounts.<name>` (NICHT state.json
    — das ist öffentlich). Mehrere Accounts via `orders.accounts` (Credentials
    als Secrets, nur deren Env-Namen stehen in der Config).

    STAGGERING: pro Bot-Lauf wird höchstens EIN Account eingeloggt (der am
    längsten überfällige). So loggen sich nie zwei Accounts gleichzeitig von
    derselben IP ein — verhindert, dass BG die Konten korreliert.
    """
    token = os.environ.get("GIST_TOKEN", "")
    gist_id = os.environ.get("GIST_ID", "")
    if not (token and gist_id):
        log.info("orders: GIST_TOKEN/GIST_ID fehlen — übersprungen")
        return

    orders_cfg = cfg.get("orders") or {}
    interval = int(orders_cfg.get("check_interval_minutes", 240))
    idle = int(orders_cfg.get("idle_interval_minutes", 1440))
    cfg_accounts = orders_cfg.get("accounts") or [
        {"name": "default", "username_env": "BG_USERNAME", "password_env": "BG_PASSWORD"}
    ]

    st = orders.load_order_state(token, gist_id)
    # Migration: alter flacher Stand (Single-Account) → unter erstem Account-Namen.
    if "orders" in st and "accounts" not in st:
        st["accounts"] = {cfg_accounts[0]["name"]: {
            "orders": st.pop("orders"),
            "last_check_at": st.pop("last_check_at", ""),
            "_initialized": st.pop("_initialized", False),
        }}
    accounts_state = st.setdefault("accounts", {})

    # Beim ersten Mal die on/off-Schalter ins Gist schreiben, damit man sie nur
    # noch umschreiben muss (kein Tippen von Klammern/Struktur).
    if "enabled" not in st:
        st["enabled"] = {a["name"]: "off" for a in cfg_accounts}
        orders.save_order_state(token, gist_id, st)
        log.info("orders: enabled-Schalter im Gist angelegt (alle 'off') — bei Bestellung auf 'on' setzen")
        return

    # An/Aus-Schalter: nur aktivierte Konten (Gist-Feld `enabled`) → in
    # Bestellpausen keine Logins. Standard = aus.
    enabled_names = [a["name"] for a in cfg_accounts if _order_enabled(st, a["name"])]
    if not enabled_names:
        log.info("orders: alle Konten 'off' (Gist `enabled`) — übersprungen.")
        return

    # Nur aktivierte Accounts mit vorhandenen Secrets (Login + Ziel-Webhook).
    resolved = []
    for a in cfg_accounts:
        if a["name"] not in enabled_names:
            continue
        user = os.environ.get(a.get("username_env", "BG_USERNAME"), "")
        pw = os.environ.get(a.get("password_env", "BG_PASSWORD"), "")
        if not (user and pw):
            continue
        webhook = os.environ.get(a["webhook_env"], "") if a.get("webhook_env") else default_webhook
        if not webhook:
            continue
        ping = _parse_ids(os.environ.get(a["ping_env"], "")) if a.get("ping_env") else default_ping_ids
        resolved.append({"name": a["name"], "user": user, "pw": pw, "webhook": webhook, "ping": ping})

    if not resolved:
        log.info("orders: aktiviert, aber Secrets/Webhook fehlen — übersprungen")
        return

    # Fällige Accounts sammeln, dann nur den am längsten überfälligen prüfen.
    due = [r for r in resolved if _orders_due(accounts_state.setdefault(r["name"], {}), interval, idle)]
    if not due:
        log.info("orders: nichts fällig (%d Account(s))", len(resolved))
        return
    due.sort(key=lambda r: accounts_state[r["name"]].get("last_check_at", ""))  # "" zuerst, dann ältester
    pick = due[0]

    acct = accounts_state[pick["name"]]
    try:
        _check_one_account(pick["name"], pick["webhook"], pick["user"], pick["pw"],
                           pick["ping"], role_ids, acct)
    except Exception as e:  # Login/Incapsula/Netzwerk — nie den ganzen Bot reißen
        log.error("orders[%s]: fetch fehlgeschlagen: %s", pick["name"], e)
    # Immer speichern: last_check_at (in _check_one_account vor dem Login gesetzt)
    # muss persistiert werden — auch bei Fehler → Backoff statt Hämmern.
    orders.save_order_state(token, gist_id, st)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_yaml(CONFIG_PATH)
    state = load_state()
    webhook_env = cfg.get("discord_webhook_env", "DISCORD_WEBHOOK_URL")
    webhook = os.environ.get(webhook_env, "")
    if not webhook:
        log.warning("env var %s is empty — Discord disabled", webhook_env)

    updates_webhook_env = cfg.get("discord_updates_webhook_env", "DISCORD_UPDATES_WEBHOOK_URL")
    updates_webhook = os.environ.get(updates_webhook_env, "")

    forum_webhook_env = cfg.get("discord_forum_webhook_env", "DISCORD_FORUM_WEBHOOK_URL")
    forum_webhook = os.environ.get(forum_webhook_env, "")

    # Stock-alerts (restock + OOS) ideally go to the dedicated bg-notify
    # channel. Fall back to the main webhook so restock alerts don't go silent
    # if the new secret hasn't been configured yet.
    stock_webhook_env = cfg.get("discord_stock_webhook_env", "DISCORD_STOCK_WEBHOOK_URL")
    stock_webhook = os.environ.get(stock_webhook_env, "") or webhook

    statuses, restocks, oos_alerts = check_products(cfg, state)
    usd_eur = fetch_usd_eur_rate()
    log.info("USD->EUR rate: %s", usd_eur)
    labels = _variant_labels(cfg)

    announce_deploy(state, updates_webhook)

    new_forum_posts = check_forum(cfg, state)
    if new_forum_posts and forum_webhook:
        for post in new_forum_posts:
            notify.send_forum_post(forum_webhook, build_forum_embed(post))
    elif new_forum_posts:
        log.info("forum: %d new post(s) but %s is empty", len(new_forum_posts), forum_webhook_env)

    # Bestellstatus aus dem BG-Kundenkonto (eigener Webhook, eigener Gist-Stand).
    order_webhook_env = cfg.get("discord_order_webhook_env", "DISCORD_ORDER_WEBHOOK_URL")
    order_webhook = os.environ.get(order_webhook_env, "")
    order_role_ids = [str(r) for r in ((cfg.get("notifications") or {}).get("ping_role_ids") or [])]
    check_orders(cfg, order_webhook, load_ping_user_ids(cfg), order_role_ids)

    if webhook:
        notif = cfg.get("notifications") or {}
        user_ids = load_ping_user_ids(cfg)
        role_ids = [str(r) for r in (notif.get("ping_role_ids") or [])]

        new_stats_id = notify.edit_in_place(
            webhook,
            build_stats_embed(cfg, state, usd_eur=usd_eur),
            message_id=state.get("stats_message_id", ""),
        )
        if new_stats_id:
            state["stats_message_id"] = new_stats_id

        new_id = notify.edit_in_place(
            webhook,
            build_dashboard_embed(statuses, usd_eur=usd_eur, labels=labels),
            message_id=state.get("dashboard_message_id", ""),
        )
        if new_id:
            state["dashboard_message_id"] = new_id

        for r in restocks:
            notify.send_restock_alert(
                stock_webhook, build_restock_embed(r, usd_eur=usd_eur, labels=labels), user_ids, role_ids,
            )
        for o in oos_alerts:
            notify.send_oos_alert(stock_webhook, build_oos_embed(o, usd_eur=usd_eur, labels=labels))

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
