"""Forum-Watcher: neue BG-Posts auf thinksteroids.com erkennen.

Gated den (teuren, Playwright-basierten) Scrape auf ein konfigurierbares
Intervall und diffed gegen die gesehenen Post-IDs in state.json.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import forum

log = logging.getLogger(__name__)


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
