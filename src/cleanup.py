"""Bulk-delete leftover Discord messages by ID.

Triggered via the `cleanup.yml` workflow with a comma-separated list of message
IDs (e.g. "1506482583964483675, 1506484639060197546"). The bot tries to DELETE
each via the webhook — 404s are silently ignored (already gone), other failures
get logged.

Used to clean up orphan dashboard messages left over from earlier race
conditions, where we lost track of their IDs.
"""
from __future__ import annotations

import logging
import os
import re
import sys

from . import notify

log = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: python -m src.cleanup '<id1>,<id2>,...'")
        return 2
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook:
        log.error("DISCORD_WEBHOOK_URL is empty")
        return 1

    ids_raw = sys.argv[1]
    # Accept comma/space/newline as separators; keep only digit-runs (Discord snowflakes are numeric).
    ids = re.findall(r"\d{15,21}", ids_raw)
    if not ids:
        log.error("no valid message IDs in input: %r", ids_raw)
        return 1

    deleted = 0
    for mid in ids:
        ok = notify.delete_message(webhook, mid)
        log.info("delete %s: %s", mid, "ok" if ok else "FAILED")
        if ok:
            deleted += 1
    log.info("deleted %s of %s", deleted, len(ids))
    return 0 if deleted else 1


if __name__ == "__main__":
    sys.exit(main())
