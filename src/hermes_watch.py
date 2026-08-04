"""Manuell eingetragene Hermes-Sendungen verfolgen.

Für Pakete, deren Bestellung der Bot nicht sehen kann (z.B. Konto eines
Kollegen ist gar nicht hinterlegt): Link von Hand ins **private Gist**, den
Rest macht der Bot. Bewusst nicht in `config.yml` — das Repo ist öffentlich.

Gist-Format (`manual_tracking`), Label frei wählbar — zwei Schreibweisen:

    "manual_tracking": {
      "Ich #37143":   "https://tracking.hermesworld.com/?TrackID=...",
      "Kollege #123": {
        "url":  "https://tracking.hermesworld.com/?TrackID=...",
        "ping": "123456789012345678"
      }
    }

Ohne `ping` wird wie gewohnt gepingt (Standard-IDs). Mit `ping` markiert die
Nachricht genau diese Discord-ID(s) — so bekommt jeder nur seine eigene Sendung
angezeigt. Mehrere IDs mit Komma trennen.

Den Rest verwaltet der Bot selbst unter `manual_tracking_state`. Ist eine
Sendung zugestellt, wird sie nicht mehr abgefragt (Eintrag kann dann weg).
"""
from __future__ import annotations

import logging
import os
import random
from datetime import datetime, timezone

from . import hermes, notify, orders
from .config import parse_ids
from .embeds import build_shipment_embed

log = logging.getLogger(__name__)

_DEFAULT_INTERVAL = 60   # Minuten zwischen zwei Abfragen derselben Sendung


def _parse_entry(val) -> tuple[str, list[str] | None]:
    """Gist-Eintrag auflösen → (url, ping_ids oder None für Standard).

    Erlaubt ist der reine URL-String oder ein Dict mit `url` und optional `ping`.
    """
    if isinstance(val, str):
        return val, None
    if isinstance(val, dict):
        url = val.get("url") or val.get("link") or ""
        raw = val.get("ping") or val.get("ping_id") or ""
        ids = parse_ids(str(raw)) if raw else None
        return url, (ids or None)
    return "", None


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
    for label, val in manual.items():
        url, own_ping = _parse_entry(val)
        if not url.startswith("http"):
            log.warning("hermes: '%s' hat keine gültige URL — übersprungen", label)
            continue
        entry = states.setdefault(label, {})
        if hermes.is_terminal(entry.get("status", "")):
            continue                      # zugestellt → nicht weiter pollen
        if _due(entry, interval):
            due.append((label, url, entry, own_ping))

    if not due:
        return

    due.sort(key=lambda t: t[2].get("last_check_at", ""))   # "" zuerst, dann ältester
    label, url, entry, own_ping = due[0]
    targets = own_ping if own_ping is not None else ping_ids

    entry["last_check_at"] = datetime.now(timezone.utc).isoformat()   # vor der Abfrage → Backoff
    data = hermes.fetch_shipment(url)

    if data:
        events = data.get("events") or []          # bereits chronologisch sortiert
        # Abgleich über Fingerabdrücke statt über Position/Anzahl: so ist es egal,
        # in welcher Reihenfolge Hermes liefert, und unbekannte Meldungstexte
        # funktionieren automatisch mit.
        seen: list[str] = list(entry.get("seen_keys") or [])
        known = set(seen)
        new_events = [e for e in events if hermes.event_key(e) not in known]

        if new_events:
            first = not seen
            notify.send_order_update(
                webhook, build_shipment_embed(label, data, new_events, url, first=first),
                targets, role_ids,
            )
            # Nur die Fingerabdrücke der aktuell sichtbaren Ereignisse behalten —
            # so wächst der Gist nicht unbegrenzt.
            entry["seen_keys"] = [hermes.event_key(e) for e in events]
            entry["status"] = data.get("summary", "")
            if data.get("number"):
                entry["number"] = data["number"]
            log.info("hermes: '%s' → %d neue(s) Ereignis(se), zuletzt: %s",
                     label, len(new_events), new_events[-1].get("text", "")[:60])
        else:
            log.info("hermes: '%s' unverändert (%d Ereignisse)", label, len(events))

    orders.save_order_state(token, gist_id, st)
