"""Orchestrator: check products, update Discord dashboard, alert on restocks.

- Dashboard: persistent embed, silently edited in place every run. Never spams.
- Restock alert: one fresh embed message per restocked variant, with @-mentions.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import bgpharma, notify

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yml"
STATE_PATH = ROOT / "state.json"

COLOR_IN_STOCK = 0x57F287    # Discord native green
COLOR_OUT      = 0x95A5A6    # Discord native gray
COLOR_WARN     = 0xFEE75C    # Discord native yellow


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

            statuses.append({
                "product_name": name,
                "product_url": url,
                "variant": variant,
                "in_stock": in_stock_now,
                "price": info.get("price", ""),
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
                    "variant": variant, "price": info.get("price", ""),
                })
            elif not in_stock_now and in_stock_prev:
                log.info("[%s] %s: out of stock again", name, variant)

            entry["in_stock"] = in_stock_now
            entry["price"] = info.get("price", "")
            entry["found"] = info["found"]

    return statuses, restocks


def build_dashboard_embed(statuses: list[dict]) -> dict:
    lines: list[str] = []
    for s in statuses:
        link = s.get("deep_link") or s.get("product_url", "")
        klick = f"⠀·⠀[Klick]({link})" if link else ""
        if s.get("error"):
            lines.append(f"⚠️⠀⠀{s['variant']}⠀·⠀*check failed*")
        elif not s["found"]:
            lines.append(f"⚠️⠀⠀{s['variant']}⠀·⠀*nicht gefunden*")
        elif s["in_stock"]:
            price = f"⠀·⠀**{s['price']}**" if s["price"] else ""
            lines.append(f"🟢⠀⠀{s['variant']}{price}{klick}")
        else:
            lines.append(f"🔴⠀⠀{s['variant']}{klick}")

    any_in_stock = any(s["in_stock"] for s in statuses if not s.get("error"))
    any_error = any(s.get("error") or not s.get("found") for s in statuses)
    color = COLOR_WARN if any_error and not any_in_stock else (COLOR_IN_STOCK if any_in_stock else COLOR_OUT)

    return {
        "author": {"name": "bgpharmadrugs.to", "url": "https://bgpharmadrugs.to/"},
        "title": "BG Pharma · Status",
        "color": color,
        "description": "\n".join(lines) if lines else "_keine Produkte konfiguriert_",
        "footer": {"text": "Letzter Check"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_restock_embed(restock: dict) -> dict:
    price_line = f"### ⠀{restock['price']}\n\n" if restock.get("price") else ""
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

    if webhook:
        notif = cfg.get("notifications") or {}
        user_ids = [str(u) for u in (notif.get("ping_user_ids") or [])]
        role_ids = [str(r) for r in (notif.get("ping_role_ids") or [])]

        new_id = notify.update_dashboard(
            webhook,
            build_dashboard_embed(statuses),
            old_message_id=state.get("dashboard_message_id", ""),
        )
        if new_id:
            state["dashboard_message_id"] = new_id

        for r in restocks:
            notify.send_restock_alert(webhook, build_restock_embed(r), user_ids, role_ids)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
