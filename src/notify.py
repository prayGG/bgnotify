"""Discord webhook notifications.

Two message types, each with a focused job:

- `edit_in_place(embed, message_id)` — persistent dashboards (status + stats),
  PATCHed silently each run. Discord doesn't push notifications for edits, so
  this never spams. POSTs a fresh message if the saved id is missing or 404.

- `send_restock_alert(embed, user_ids)` — one new POST per restocked variant.
  Includes `<@user_id>` in the content so Discord pings the listed users.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional

import requests

log = logging.getLogger(__name__)


def _request(method: str, url: str, payload: Optional[dict] = None, *, quiet_404: bool = False) -> Optional[dict]:
    try:
        r = requests.request(method, url, json=payload if payload is not None else None, timeout=15)
        if r.status_code == 404 and quiet_404:
            return {}
        if r.status_code >= 400:
            log.error("discord %s %s: %s", method, r.status_code, r.text[:300])
            return None
        if not r.content:
            return {}
        return r.json()
    except requests.RequestException as e:
        log.error("discord %s failed: %s", method, e)
        return None


def _mentions(user_ids: list[str], role_ids: list[str]) -> tuple[str, dict]:
    user_ids = [str(u) for u in user_ids]
    role_ids = [str(r) for r in role_ids]
    parts = [f"<@{u}>" for u in user_ids] + [f"<@&{r}>" for r in role_ids]
    return " ".join(parts), {"users": user_ids, "roles": role_ids}


def edit_in_place(webhook_url: str, embed: dict, message_id: str = "") -> Optional[str]:
    """Update one persistent message silently. POST on first run, PATCH after.

    Falls back to POST on 404 so a manually-deleted message self-heals. Webhook
    edits don't push Discord notifications, so this can be called every run.
    """
    if not webhook_url:
        return None
    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}

    if message_id:
        result = _request("PATCH", f"{webhook_url}/messages/{message_id}", payload, quiet_404=True)
        if result is None:
            return message_id
        if result.get("id"):
            return str(result["id"])

    post_payload = {"username": "bgnotify by pray", **payload}
    result = _request("POST", f"{webhook_url}?wait=true", post_payload)
    if result is None:
        return None
    return str(result.get("id") or "") or None


def send_restock_alert(
    webhook_url: str,
    embed: dict,
    user_ids: Optional[list[str]] = None,
    role_ids: Optional[list[str]] = None,
) -> bool:
    if not webhook_url:
        return False
    prefix, allowed = _mentions(user_ids or [], role_ids or [])
    payload = {
        "username": "bgnotify by pray",
        "content": prefix,
        "embeds": [embed],
        "allowed_mentions": allowed,
    }
    return _request("POST", webhook_url, payload) is not None


def send(webhook_url: str, content: str) -> bool:
    """Simple unpinged message — CLI test only."""
    if not webhook_url:
        return False
    payload = {"username": "bgnotify by pray", "content": content[:1900], "allowed_mentions": {"parse": []}}
    return _request("POST", webhook_url, payload) is not None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    msg = " ".join(sys.argv[1:]) or "Test message from bgnotify"
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    ok = send(url, msg)
    sys.exit(0 if ok else 1)
