"""Send a TEST restock alert for every configured variant.

Triggered via the `test-ping.yml` workflow in GitHub Actions — no need to run
the bot locally to verify the @-mention setup. Each variant gets one alert
embed clearly marked as a test (yellow sidebar, 🧪 header).
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import notify

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yml"

COLOR_TEST = 0xFEE75C  # Discord yellow


def build_test_embed(product_name: str, variant: str, link: str) -> dict:
    return {
        "author": {"name": "🧪⠀⠀TEST PING⠀⠀🧪"},
        "title": variant,
        "description": (
            "_Dies ist ein Test der Ping- und Embed-Funktion — keine echte Verfügbarkeit._\n\n"
            f"**[→⠀⠀Zum Produkt]({link})**"
        ),
        "color": COLOR_TEST,
        "footer": {"text": product_name},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


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

    sent = 0
    for product in cfg.get("products") or []:
        for variant in product.get("watch_variants") or []:
            embed = build_test_embed(
                product_name=product.get("name") or variant,
                variant=variant,
                link=product["url"],
            )
            ok = notify.send_restock_alert(webhook, embed, user_ids, role_ids)
            log.info("test ping %s: %s", variant, "ok" if ok else "FAILED")
            if ok:
                sent += 1
    log.info("sent %s test alerts", sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
