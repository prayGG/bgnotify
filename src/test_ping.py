"""Send a restock alert per configured variant — using live BG Pharma data
and the IDENTICAL embed/format as a real restock.

Triggered via the `test-ping.yml` workflow in GitHub Actions. Use this to verify
that:
1. The @-mentions (`ping_user_ids` in config) are valid Discord IDs.
2. The visual design matches what you'll see when a real restock happens.

If the embed arrives but you don't get pinged: your User-ID in config is wrong.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import yaml

from . import bgpharma, notify
from .main import build_restock_embed, fetch_usd_eur_rate

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yml"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    webhook_env = cfg.get("discord_webhook_env", "DISCORD_WEBHOOK_URL")
    webhook = os.environ.get(webhook_env, "")
    if not webhook:
        log.error("env var %s is empty", webhook_env)
        return 1

    notif = cfg.get("notifications") or {}
    user_ids = [str(u) for u in (notif.get("ping_user_ids") or [])]
    role_ids = [str(r) for r in (notif.get("ping_role_ids") or [])]
    log.info("pinging users=%s roles=%s", user_ids, role_ids)

    usd_eur = fetch_usd_eur_rate()

    sent = 0
    for product in cfg.get("products") or []:
        url = product["url"]
        name = product.get("name") or url
        watch = product.get("watch_variants") or []
        if not watch:
            continue
        try:
            current = bgpharma.check(url, watch)
        except Exception as e:
            log.error("live check failed for %s: %s — sending fallback embed", url, e)
            current = {v: {"deep_link": url, "price": ""} for v in watch}

        for variant, info in current.items():
            restock = {
                "product_name": name,
                "product_url": url,
                "deep_link": info.get("deep_link") or url,
                "variant": variant,
                "price": info.get("price", ""),
            }
            embed = build_restock_embed(restock, usd_eur=usd_eur)
            ok = notify.send_restock_alert(webhook, embed, user_ids, role_ids)
            log.info("alert %s: %s", variant, "ok" if ok else "FAILED")
            if ok:
                sent += 1

    log.info("sent %s alerts", sent)
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
