"""Visual smoke test — posts one example of every notification type to its
proper webhook so you can confirm at a glance that:

- Each webhook actually delivers to the channel you expect (main / updates / forum)
- Embed formatting still renders correctly (no broken markup after a refactor)
- @-mentions ping the right users (`ping_user_ids` in config)
- The forum scraper still gets past Incapsula on the runner IP

Uses live data where it can — current stock + prices, real BG forum post —
and labels every message with `bgnotify · TEST` as the bot name so test
output is never confused with real signals. Reads `state.json` but never
writes it (deep-copies before passing into check_products), so running this
is safe and idempotent.

Triggered via the `test-ping` workflow.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import yaml

from . import bgpharma, forum, notify  # noqa: F401  (bgpharma used transitively)
from .main import (
    _variant_labels,
    build_dashboard_embed,
    build_forum_embed,
    build_oos_embed,
    build_restock_embed,
    build_stats_embed,
    build_updates_embed,
    check_products,
    fetch_usd_eur_rate,
    load_ping_user_ids,
)

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yml"
STATE_PATH = ROOT / "state.json"


def _pick_restock_sample(statuses: list[dict]) -> Optional[dict]:
    """Return a realistic restock payload built from the first in-stock variant.

    Using a real variant means the embed shows real product names + prices —
    a better visual check than a hardcoded dummy.
    """
    for s in statuses:
        if s.get("in_stock") and s.get("found"):
            return {
                "product_name": s["product_name"],
                "product_url": s["product_url"],
                "deep_link": s.get("deep_link") or s["product_url"],
                "variant": s["variant"],
                "price": s.get("price", ""),
            }
    return None


def _pick_oos_sample(statuses: list[dict]) -> Optional[dict]:
    """First currently-OOS (but known) variant — used to render an OOS embed
    with real product naming for the visual test."""
    for s in statuses:
        if not s.get("in_stock") and s.get("found"):
            return {
                "product_name": s["product_name"],
                "product_url": s["product_url"],
                "deep_link": s.get("deep_link") or s["product_url"],
                "variant": s["variant"],
                "last_price": s.get("price", ""),
            }
    return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}

    main_wh = os.environ.get(cfg.get("discord_webhook_env", "DISCORD_WEBHOOK_URL"), "")
    updates_wh = os.environ.get(cfg.get("discord_updates_webhook_env", "DISCORD_UPDATES_WEBHOOK_URL"), "")
    forum_wh = os.environ.get(cfg.get("discord_forum_webhook_env", "DISCORD_FORUM_WEBHOOK_URL"), "")
    stock_wh = os.environ.get(cfg.get("discord_stock_webhook_env", "DISCORD_STOCK_WEBHOOK_URL"), "")
    # Same fallback as production main(): if the stock webhook isn't set yet,
    # stock alerts (restock + OOS) route to the main channel.
    stock_target_wh = stock_wh or main_wh

    if not main_wh:
        log.error("DISCORD_WEBHOOK_URL is empty — nothing to test")
        return 1

    notif = cfg.get("notifications") or {}
    user_ids = load_ping_user_ids(cfg)
    role_ids = [str(r) for r in (notif.get("ping_role_ids") or [])]
    log.info("ping targets — users=%d, roles=%d", len(user_ids), len(role_ids))

    usd_eur = fetch_usd_eur_rate()
    log.info("USD->EUR rate: %s", usd_eur)

    # Read state, deep-copy before passing into check_products so we never
    # accidentally write test-run mutations back to disk.
    on_disk_state: dict = {}
    if STATE_PATH.exists():
        try:
            on_disk_state = json.loads(STATE_PATH.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError as e:
            log.warning("state.json unreadable, using empty state: %s", e)

    state_copy = copy.deepcopy(on_disk_state)
    statuses, _restocks_unused, _oos_unused = check_products(cfg, state_copy)
    labels = _variant_labels(cfg)

    results: list[tuple[str, str, object]] = []  # (test_name, channel, True | False | "skip-reason")

    # ─── 1/6 · Dashboard preview → status channel ───────────────────────────
    log.info("test 1/6 → dashboard preview (status)")
    ok = notify.send_test(
        main_wh,
        build_dashboard_embed(statuses, usd_eur=usd_eur, labels=labels),
        "🧪 **TEST** · Dashboard-Preview · _Live-Daten, postet als neue Message, "
        "die echte persistente Dashboard-Message wird NICHT überschrieben._",
    )
    results.append(("dashboard", "status", ok))

    # ─── 2/6 · Stats preview → status channel ───────────────────────────────
    log.info("test 2/6 → stats preview (status)")
    ok = notify.send_test(
        main_wh,
        build_stats_embed(cfg, state_copy, usd_eur=usd_eur),
        "🧪 **TEST** · Stats-Card-Preview · _Sparklines, OOS-Ø, Restock-Count "
        "aus echtem State._",
    )
    results.append(("stats", "status", ok))

    # ─── 3/6 · Restock alert + pings → stock channel ────────────────────────
    # The only test that includes user/role mentions — proves the IDs in
    # config are valid Discord IDs and the bot has permission to ping.
    log.info("test 3/6 → restock alert + @-mentions (stock)")
    sample = _pick_restock_sample(statuses) or {
        "product_name": "BG Pharma",
        "product_url": "https://bgpharmadrugs.to/",
        "deep_link": "https://bgpharmadrugs.to/",
        "variant": "Test-Variante",
        "price": "€42.00",
    }
    stock_channel_label = "stock" if stock_wh else "status (fallback)"
    ok = notify.send_test(
        stock_target_wh,
        build_restock_embed(sample, usd_eur=usd_eur, labels=labels),
        "🧪 **TEST** · Restock-Alert · _wenn ihr jetzt einen Discord-Ping kriegt, "
        "stimmen eure User-IDs in `notifications.ping_user_ids`._",
        user_ids=user_ids,
        role_ids=role_ids,
    )
    results.append(("restock + pings", stock_channel_label, ok))

    # ─── 4/6 · Out-of-stock alert (silent) → stock channel ──────────────────
    log.info("test 4/6 → oos alert (stock)")
    oos_sample = _pick_oos_sample(statuses) or {
        "product_name": "BG Pharma",
        "product_url": "https://bgpharmadrugs.to/",
        "deep_link": "https://bgpharmadrugs.to/",
        "variant": "Test-Variante",
        "last_price": "€42.00",
    }
    ok = notify.send_test(
        stock_target_wh,
        build_oos_embed(oos_sample, usd_eur=usd_eur, labels=labels),
        "🧪 **TEST** · OOS-Alert · _stiller Notify, kein Ping — landet im selben "
        "Channel wie Restocks aber ohne @-Mention._",
    )
    results.append(("oos (silent)", stock_channel_label, ok))

    # ─── 5/6 · Deploy announcement → updates channel ────────────────────────
    log.info("test 5/6 → deploy announcement (updates)")
    if updates_wh:
        fake_commits = [
            ("abc1234", "test: this is a deploy embed preview"),
            ("def5678", "test: verifying commit-line rendering"),
            ("9876fed", "test: third entry to check spacing"),
        ]
        ok = notify.send_test(
            updates_wh,
            build_updates_embed(fake_commits, "abc1234def56789876fed"),
            "🧪 **TEST** · Deploy-Announcement-Preview · _so sieht ein echter "
            "Deploy aus wenn HEAD-SHA in state sich ändert._",
        )
        results.append(("deploy", "updates", ok))
    else:
        log.warning("DISCORD_UPDATES_WEBHOOK_URL unset — skipping deploy test")
        results.append(("deploy", "updates", "skip-no-webhook"))

    # ─── 6/6 · Forum scraper + post embed → forum channel ───────────────────
    log.info("test 6/6 → forum scraper + post embed (forum)")
    if forum_wh:
        forum_cfg = cfg.get("forum") or {}
        url = forum_cfg.get("search_url")
        author = forum_cfg.get("author") or ""
        posts: list[dict] = []
        if url:
            try:
                posts = forum.fetch_posts(url, expected_author=author)
            except Exception as e:
                log.error("forum scrape crashed: %s", e)
        if posts:
            ok = notify.send_test(
                forum_wh,
                build_forum_embed(posts[0]),
                f"🧪 **TEST** · Forum-Post-Preview · _Scraper kam durch Incapsula, "
                f"{len(posts)} BG-Posts gefunden. Hier der neueste._",
            )
            results.append(("forum scrape+embed", "forum", ok))
        else:
            log.error("forum scrape returned 0 posts — Incapsula? Selector drift?")
            # Post a diagnostic message anyway so the channel surfaces the failure.
            notify.send_test(
                forum_wh, {}, "🧪 **TEST** · Forum-Scraper ⚠️ — 0 Posts gefunden. "
                "Check Action-Log: vermutlich Incapsula-Block oder XenForo-Layout-Drift.",
            )
            results.append(("forum scrape+embed", "forum", "scrape-empty"))
    else:
        log.warning("DISCORD_FORUM_WEBHOOK_URL unset — skipping forum test")
        results.append(("forum scrape+embed", "forum", "skip-no-webhook"))

    # ─── Summary ────────────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("TEST SUMMARY")
    log.info("=" * 60)
    hard_fail = False
    for name, channel, outcome in results:
        if outcome is True:
            log.info("  ok        %-22s → %s", name, channel)
        elif outcome is False:
            log.error("  FAIL      %-22s → %s (webhook POST failed)", name, channel)
            hard_fail = True
        else:
            log.warning("  skipped   %-22s → %s (%s)", name, channel, outcome)
    log.info("=" * 60)
    return 1 if hard_fail else 0


if __name__ == "__main__":
    sys.exit(main())
