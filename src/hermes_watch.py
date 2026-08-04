"""Manuell eingetragene Hermes-Sendungen verfolgen.

Für Pakete, deren Bestellung der Bot nicht sehen kann (z.B. Konto eines
Kollegen ist gar nicht hinterlegt): Link von Hand ins **private Gist**, den
Rest macht der Bot. Bewusst nicht in `config.yml` — das Repo ist öffentlich.

Gist-Format (`manual_tracking`), Label frei wählbar:

    "manual_tracking": {
      "Kollege #123": "https://tracking.hermesworld.com/?TrackID=..."
    }

Den Rest verwaltet der Bot selbst unter `manual_tracking_state`. Ist eine
Sendung zugestellt, wird sie nicht mehr abgefragt (Eintrag kann dann weg).
"""
from __future__ import annotations

import logging
import os
import random
from datetime import datetime, timezone

from . import hermes, notify, orders
from .embeds import build_shipment_embed

log = logging.getLogger(__name__)

_DEFAULT_INTERVAL = 60   # Minuten zwischen zwei Abfragen derselben Sendung


def _due(entry: dict, interval_minutes: int) -> bool:
    """Fällig? Kein last_check_at = noch nie geprüft = fällig. ±10% Jitter."""
    last = entry.get("last_check_at", "")
    if not last:
        return True
    try:
        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return True
    threshold = interval_minutes * 60 * random.uniform(0.9, 1.1)
    return (datetime.now(timezone.utc) - dt).total_seconds() >= threshold


def check_shipments(cfg: dict, webhook: str, ping_ids: list[str], role_ids: list[str]) -> None:
    """Alle Einträge aus `manual_tracking` prüfen und Änderungen posten.

    Pro Lauf wird höchstens EINE Sendung abgefragt (Playwright ist teuer und
    wir wollen Hermes nicht hämmern) — die am längsten überfällige.
    """
    token = os.environ.get("GIST_TOKEN", "")
    gist_id = os.environ.get("GIST_ID", "")
    if not (token and gist_id):
        return
    if not webhook:
        log.info("hermes: kein Order-Webhook — übersprungen")
        return

    interval = int(((cfg.get("tracking") or {}).get("check_interval_minutes")) or _DEFAULT_INTERVAL)

    st = orders.load_order_state(token, gist_id)
    manual = st.get("manual_tracking") or {}
    if not manual:
        return

    states = st.setdefault("manual_tracking_state", {})

    # Fällige, noch nicht abgeschlossene Sendungen sammeln.
    due = []
    for label, url in manual.items():
        if not isinstance(url, str) or not url.startswith("http"):
            log.warning("hermes: '%s' ist keine URL — übersprungen", label)
            continue
        entry = states.setdefault(label, {})
        if hermes.is_terminal(entry.get("status", "")):
            continue                      # zugestellt → nicht weiter pollen
        if _due(entry, interval):
            due.append((label, url, entry))

    if not due:
        return

    due.sort(key=lambda t: t[2].get("last_check_at", ""))   # "" zuerst, dann ältester
    label, url, entry = due[0]

    entry["last_check_at"] = datetime.now(timezone.utc).isoformat()   # vor der Abfrage → Backoff
    status = hermes.fetch_status(url)

    if status:
        prev = entry.get("status", "")
        if status != prev:
            notify.send_order_update(
                webhook, build_shipment_embed(label, status, url, first=not prev),
                ping_ids, role_ids,
            )
            entry["status"] = status
            log.info("hermes: '%s' → %s", label, status)
        else:
            log.info("hermes: '%s' unverändert (%s)", label, status)

    orders.save_order_state(token, gist_id, st)
