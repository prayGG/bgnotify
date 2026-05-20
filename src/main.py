"""Orchestrator: check products, update Discord dashboard, alert on restocks.

- Dashboard: persistent embed, silently edited in place every run. Never spams.
- Restock alert: one fresh embed message per restocked variant, with @-mentions.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml

from . import bgpharma, notify

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


def save_state(state: dict) -> None:
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def check_products(cfg: dict, state: dict) -> tuple[list[dict], list[dict]]:
    products_state = state.setdefault("products", {})
    bot_stats = state.setdefault("bot_stats", {})
    run_iso = datetime.now(timezone.utc).isoformat()
    bot_stats.setdefault("first_check_at", run_iso)
    bot_stats["last_check_at"] = run_iso
    bot_stats["total_checks"] = bot_stats.get("total_checks", 0) + 1
    statuses: list[dict] = []
    restocks: list[dict] = []

    for product in cfg.get("products") or []:
        url = product["url"]
        name = product.get("name") or url
        watch = product.get("watch_variants") or []
        prev = products_state.setdefault(url, {})
        try:
            current = bgpharma.check(url, watch)
        except Exception as e:
            log.error("check failed for %s: %s", url, e)
            for variant in watch or ["(unknown)"]:
                statuses.append({
                    "product_name": name, "product_url": url, "variant": variant,
                    "in_stock": False, "price": "", "found": False,
                    "deep_link": url, "error": True,
                })
            continue

        for variant, info in current.items():
            in_stock_now = bool(info["in_stock"])
            entry = prev.setdefault(variant, {})
            in_stock_prev = entry.get("in_stock")
            deep = info.get("deep_link") or url
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
                "price": new_price,
                "previous_price": entry.get("previous_price", ""),
                "out_since": entry.get("out_since", ""),
                "found": info.get("found", False),
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
                log.info("[%s] %s: out of stock again", name, variant)

            entry["in_stock"] = in_stock_now
            if new_price:
                entry["price"] = new_price
            entry["found"] = info["found"]

    return statuses, restocks


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


def _avg_oos_duration(periods: list) -> str:
    total = 0.0
    count = 0
    for p in periods or []:
        try:
            start = datetime.fromisoformat(p["start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(p["end"].replace("Z", "+00:00"))
        except (ValueError, KeyError, TypeError):
            continue
        total += (end - start).total_seconds()
        count += 1
    if not count:
        return "—"
    return _humanize_duration(total / count)


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


def build_dashboard_embed(statuses: list[dict], usd_eur: Optional[float] = None) -> dict:
    blocks: list[str] = []
    for s in statuses:
        link = s.get("deep_link") or s.get("product_url", "")
        klick = f"⠀·⠀[Klick]({link})" if link else ""

        if s.get("error"):
            sub = f"⚠️⠀check failed"
        elif not s["found"]:
            sub = f"⚠️⠀nicht gefunden"
        elif s["in_stock"]:
            shown_price = display_price(s.get("price", ""), usd_eur)
            shown_prev = display_price(s.get("previous_price", ""), usd_eur)
            price = f"⠀·⠀**{shown_price}**" if shown_price else ""
            delta = _price_change_suffix(shown_prev, shown_price)
            sub = f"🟢⠀in stock{price}{delta}"
        else:
            shown_last = display_price(s.get("price", ""), usd_eur)
            last_suffix = f"⠀·⠀_zuletzt {shown_last}_" if shown_last else ""
            sub = f"🔴⠀out of stock{last_suffix}"

        blocks.append(f"**{s['variant']}**\n└⠀{sub}{klick}")

    any_in_stock = any(s["in_stock"] for s in statuses if not s.get("error"))
    any_error = any(s.get("error") or not s.get("found") for s in statuses)
    color = COLOR_WARN if any_error and not any_in_stock else (COLOR_IN_STOCK if any_in_stock else COLOR_OUT)

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
    bot_lines = ["📊⠀**Bot**"]
    for i, line in enumerate(bot_sub):
        prefix = "└⠀" if i == len(bot_sub) - 1 else "├⠀"
        bot_lines.append(prefix + line)
    blocks = ["\n".join(bot_lines)]

    for product in cfg.get("products") or []:
        url = product["url"]
        watch = product.get("watch_variants") or []
        emoji = product.get("emoji") or "💊"
        product_data = products_state.get(url, {})
        for variant in watch:
            e = product_data.get(variant)
            if not e:
                continue
            sample_price = e.get("price", "") or e.get("lowest_price", "")
            lines = [f"{emoji}⠀**{variant}**"]

            # Preisverlauf — prominent: sparkline + endpoints
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

            # OOS-Dauer Ø — prominent
            avg = _avg_oos_duration(e.get("oos_periods", []))
            lines.append(f"├⠀⏱⠀**OOS-Dauer Ø: {avg}**")

            # Tief / hoch (Kontext, je eigene Zeile)
            low = display_price(e.get("lowest_price", ""), usd_eur)
            high = display_price(e.get("highest_price", ""), usd_eur)
            low_ago = _humanize_ago(e.get("lowest_price_at", ""))
            high_ago = _humanize_ago(e.get("highest_price_at", ""))
            if low:
                lines.append(f"├⠀tief {low}" + (f" ({low_ago})" if low_ago else ""))
            if high:
                lines.append(f"├⠀hoch {high}" + (f" ({high_ago})" if high_ago else ""))

            # Restocks (Abschluss)
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


def build_restock_embed(restock: dict, usd_eur: Optional[float] = None) -> dict:
    shown = display_price(restock.get("price", ""), usd_eur)
    price_line = f"### ⠀{shown}\n\n" if shown else ""
    return {
        "author": {"name": "✦⠀⠀RESTOCKED⠀⠀✦"},
        "title": restock["variant"],
        "description": (
            price_line
            + f"**[→⠀⠀Jetzt bestellen]({restock['deep_link']})**"
        ),
        "color": COLOR_IN_STOCK,
        "footer": {"text": "bgpharmadrugs.to"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_yaml(CONFIG_PATH)
    state = load_state()
    webhook_env = cfg.get("discord_webhook_env", "DISCORD_WEBHOOK_URL")
    webhook = os.environ.get(webhook_env, "")
    if not webhook:
        log.warning("env var %s is empty — Discord disabled", webhook_env)

    statuses, restocks = check_products(cfg, state)
    usd_eur = fetch_usd_eur_rate()
    log.info("USD->EUR rate: %s", usd_eur)

    if webhook:
        notif = cfg.get("notifications") or {}
        user_ids = [str(u) for u in (notif.get("ping_user_ids") or [])]
        role_ids = [str(r) for r in (notif.get("ping_role_ids") or [])]

        new_stats_id = notify.edit_in_place(
            webhook,
            build_stats_embed(cfg, state, usd_eur=usd_eur),
            message_id=state.get("stats_message_id", ""),
        )
        if new_stats_id:
            state["stats_message_id"] = new_stats_id

        new_id = notify.update_dashboard(
            webhook,
            build_dashboard_embed(statuses, usd_eur=usd_eur),
            old_message_id=state.get("dashboard_message_id", ""),
        )
        if new_id:
            state["dashboard_message_id"] = new_id

        for r in restocks:
            notify.send_restock_alert(webhook, build_restock_embed(r, usd_eur=usd_eur), user_ids, role_ids)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
