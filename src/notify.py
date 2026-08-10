"""Discord webhook notifications — the only module that talks to Discord.

Two delivery patterns, each with a focused job:

- `edit_in_place(embed, message_id)` — persistent dashboards (status + stats),
  PATCHed silently each run. Discord doesn't push notifications for edits, so
  this never spams. POSTs a fresh message if the saved id is missing or 404.

- `send_*` — one new POST per event. Pinging variants (restock, order update) include `<@user_id>`/`<@&role_id>` mentions in the content;
  silent variants (OOS, forum post, deploy announcement) never mention anyone.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3


def _request(method: str, url: str, payload: Optional[dict] = None, *, quiet_404: bool = False) -> Optional[dict]:
    """Send Discord webhook request with retries for 429/5xx/network errors.

    Returns parsed JSON on success, {} on 404 (when quiet_404) or empty body,
    None when all attempts failed. Retries respect Discord's `retry_after` on
    429 and use exponential backoff on 5xx and network errors.
    """
    for attempt in range(_MAX_ATTEMPTS):
        try:
            r = requests.request(method, url, json=payload if payload is not None else None, timeout=15)
        except requests.RequestException as e:
            if attempt < _MAX_ATTEMPTS - 1:
                wait = 2 ** attempt
                log.warning("discord %s network error, retry in %ds: %s", method, wait, e)
                time.sleep(wait)
                continue
            log.error("discord %s failed: %s", method, e)
            return None

        if r.status_code == 404 and quiet_404:
            return {}
        if r.status_code == 429 and attempt < _MAX_ATTEMPTS - 1:
            try:
                wait = float(r.json().get("retry_after", 1.0))
            except (ValueError, requests.JSONDecodeError):
                wait = 1.0
            log.warning("discord rate-limited, sleeping %.2fs", wait)
            time.sleep(min(wait, 10.0))
            continue
        if 500 <= r.status_code < 600 and attempt < _MAX_ATTEMPTS - 1:
            wait = 2 ** attempt
            log.warning("discord %s %s, retry in %ds", method, r.status_code, wait)
            time.sleep(wait)
            continue
        if r.status_code >= 400:
            log.error("discord %s %s: %s", method, r.status_code, r.text[:300])
            return None
        if not r.content:
            return {}
        return r.json()
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

    post_payload = {"username": "bgnotify · status", **payload}
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


def send_oos_alert(webhook_url: str, embed: dict) -> bool:
    """Silent out-of-stock alert — no pings. Gleicher Channel-Name wie Restock
    (bg-notify); die Embeds (RESTOCKED/OUT OF STOCK) unterscheiden sich eh."""
    if not webhook_url:
        return False
    payload = {
        "username": "bgnotify by pray",
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }
    return _request("POST", webhook_url, payload) is not None


def send_update_announcement(webhook_url: str, embed: dict) -> bool:
    """One-shot deploy/update announcement to the updates channel."""
    if not webhook_url:
        return False
    payload = {
        "username": "bgnotify · updates",
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }
    return _request("POST", webhook_url, payload) is not None


def send_forum_post(webhook_url: str, embed: dict) -> bool:
    """Silent notification for a new BG forum post — no pings."""
    if not webhook_url:
        return False
    payload = {
        "username": "bgnotify · meso",
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }
    return _request("POST", webhook_url, payload) is not None


def send_command_result(app_id: str, token: str, embed: dict) -> bool:
    """Ergebnis als Antwort auf einen Discord-Command nachreichen.

    Landet im selben Channel, in dem der Command getippt wurde, und ist nur für
    den Aufrufer sichtbar (Flag 64) — genau wie jede andere Command-Antwort.
    Deshalb kein Webhook und kein Ping: Was jemand selbst angestoßen hat, geht
    niemanden sonst etwas an.

    Der Interaction-Token IST die Berechtigung, ein Auth-Header entfällt. Er
    gilt 15 Minuten; danach antwortet Discord mit 404, was hier nur eine
    Warnung wert ist — das Ergebnis liegt im Stand des Bots und erscheint beim
    nächsten Aufruf des Commands sofort.
    """
    if not (app_id and token):
        return False
    url = f"https://discord.com/api/v10/webhooks/{app_id}/{token}"
    payload = {"embeds": [embed], "flags": 1 << 6}
    return _request("POST", url, payload, quiet_404=True) is not None


def send_order_update(
    webhook_url: str,
    embed: dict,
    user_ids: Optional[list[str]] = None,
    role_ids: Optional[list[str]] = None,
) -> bool:
    """One POST per order status change / tracking drop, into the private
    order channel. Pings the order's owner so they actually notice."""
    if not webhook_url:
        return False
    prefix, allowed = _mentions(user_ids or [], role_ids or [])
    payload = {
        "username": "bgnotify · orders",
        "content": prefix,
        "embeds": [embed],
        "allowed_mentions": allowed,
    }
    return _request("POST", webhook_url, payload) is not None


def send_test(
    webhook_url: str,
    embed: dict,
    label: str,
    user_ids: Optional[list[str]] = None,
    role_ids: Optional[list[str]] = None,
) -> bool:
    """One-shot test message — distinct `bgnotify · TEST` username, label in
    content, optional mentions (used to verify ping config end-to-end)."""
    if not webhook_url:
        return False
    prefix, allowed = _mentions(user_ids or [], role_ids or [])
    content = label
    if prefix:
        content = f"{label}\n{prefix}" if label else prefix
    payload = {
        "username": "bgnotify · TEST",
        "content": content,
        "embeds": [embed] if embed else [],
        "allowed_mentions": allowed if (user_ids or role_ids) else {"parse": []},
    }
    return _request("POST", webhook_url, payload) is not None


def send(webhook_url: str, content: str) -> bool:
    """Simple unpinged message — CLI test only."""
    if not webhook_url:
        return False
    payload = {"username": "bgnotify · status", "content": content[:1900], "allowed_mentions": {"parse": []}}
    return _request("POST", webhook_url, payload) is not None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    msg = " ".join(sys.argv[1:]) or "Test message from bgnotify"
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    ok = send(url, msg)
    sys.exit(0 if ok else 1)
