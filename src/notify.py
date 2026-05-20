"""Discord webhook notifications.

Two message types, each with a focused job:

- `update_dashboard(embed, old_id)` — persistent status board, **always edits
  in place** (silent). Discord doesn't push notifications for edits, so this
  never spams. If the old message is gone, falls back to a fresh POST.

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


def update_dashboard(webhook_url: str, embed: dict, old_message_id: str = "") -> Optional[str]:
    """Edit the existing dashboard in place; if gone, POST fresh. Returns id."""
    if not webhook_url:
        return None
    payload = {
        "username": "BG Watch",
        "embeds": [embed],
        "content": "",
        "allowed_mentions": {"parse": []},
    }
    if old_message_id:
        if _request("PATCH", f"{webhook_url}/messages/{old_message_id}", payload) is not None:
            return old_message_id
        log.info("dashboard message %s gone, recreating", old_message_id)
    result = _request("POST", f"{webhook_url}?wait=true", payload)
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
        "username": "BG Watch",
        "content": prefix,
        "embeds": [embed],
        "allowed_mentions": allowed,
    }
    return _request("POST", webhook_url, payload) is not None


def send(webhook_url: str, content: str) -> bool:
    """Simple unpinged message — CLI test only."""
    if not webhook_url:
        return False
    payload = {"username": "BG Watch", "content": content[:1900], "allowed_mentions": {"parse": []}}
    return _request("POST", webhook_url, payload) is not None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    msg = " ".join(sys.argv[1:]) or "Test message from bgnotify"
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    ok = send(url, msg)
    sys.exit(0 if ok else 1)
