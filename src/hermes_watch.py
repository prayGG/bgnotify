"""Hermes-Sendungen verfolgen — automatisch erkannte und von Hand eingetragene.

Zwei Quellen, ein Ablauf:

* `auto_tracking` — schreibt der Bestell-Watcher selbst, sobald bei einer
  Bestellung ein Tracking-Link auftaucht (dieselbe Sendung, für die auch die
  "Tracking ist da"-Karte rausging). Nichts zu tun.
* `manual_tracking` — für Pakete, deren Bestellung der Bot nicht sehen kann
  (z.B. Konto eines Kollegen ist gar nicht hinterlegt): Link von Hand ins
  **private Gist**. Bewusst nicht in `config.yml` — das Repo ist öffentlich.

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

    Erlaubt ist der reine URL-String oder ein Dict:

        "url"      Tracking-Link (Pflicht)
        "ping_env" Name eines GitHub-Secrets, z.B. "WEITERE_ID_HIER" — bevorzugt,
                   dann steht die Discord-ID nirgends im Gist
        "ping"     Discord-ID(en) direkt, mehrere mit Komma (Fallback)
    """
    if isinstance(val, str):
        return val, None
    if isinstance(val, dict):
        url = val.get("url") or val.get("link") or ""
        env_name = val.get("ping_env") or ""
        raw = os.environ.get(env_name, "") if env_name else (val.get("ping") or val.get("ping_id") or "")
        if env_name and not raw:
            log.warning("hermes: Secret '%s' ist leer/fehlt — Standard-Ping", env_name)
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
    # `auto_tracking` füllt der Bestell-Watcher selbst: sobald bei einer Bestellung
    # ein Tracking-Link auftaucht, steht die Sendung hier drin und wird ab dann
    # genauso verfolgt wie eine von Hand eingetragene. Bei gleichem Label gewinnt
    # der Handeintrag — von Hand gesetzt schlägt automatisch.
    auto = st.get("auto_tracking") or {}
    entries = {**auto, **manual}
    if not entries:
        return

    states = st.setdefault("manual_tracking_state", {})
    delivered: list[str] = []

    # Fällige, noch nicht abgeschlossene Sendungen sammeln.
    due = []
    for label, val in entries.items():
        url, own_ping = _parse_entry(val)
        if not url.startswith("http"):
            log.warning("hermes: '%s' hat keine gültige URL — übersprungen", label)
            continue
        entry = states.setdefault(label, {})
        # Neuer Link unter altem Label = neue Sendung -> Stand zurücksetzen.
        # Sonst würde ein "zugestellt" der Vorsendung die neue dauerhaft blockieren.
        if entry.get("url") and entry["url"] != url:
            log.info("hermes: '%s' hat einen neuen Link — Stand zurückgesetzt", label)
            entry.clear()
        entry["url"] = url
        if hermes.is_terminal(entry.get("status", "")):
            # Zugestellt → nicht weiter pollen. Automatisch eingetragene Sendungen
            # fliegen danach raus (das Gist soll nicht mit alten Paketen volllaufen);
            # `tracking_posted` im Bestellstand verhindert ein Wieder-Eintragen.
            if label in auto and label not in manual:
                delivered.append(label)
            continue
        if _due(entry, interval):
            due.append((label, url, entry, own_ping))

    for label in delivered:
        auto.pop(label, None)
        states.pop(label, None)
        log.info("hermes: '%s' zugestellt — Eintrag aufgeräumt", label)

    if not due:
        if delivered:
            orders.save_order_state(token, gist_id, st)
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
