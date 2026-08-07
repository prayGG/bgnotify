"""Wunschzustand aus `commands.json` — was per Discord-Command gesetzt wurde.

Der Cloudflare-Worker nimmt Slash-Commands entgegen und legt das Ergebnis in
einer **eigenen** Gist-Datei ab. Er fasst `order-state.json` bewusst nie an:
Der Bot arbeitet darauf nach dem Muster laden → ändern → speichern, ein
Fremdschreiber würde jede Änderung verlieren, die zufällig zwischen dem Laden
und dem Speichern entsteht. Getrennte Dateien schließen das baulich aus.

Abgelegt wird ein **Wunschzustand**, keine Auftragsliste:

    {
      "enabled":  {"a": "off"},
      "tracking": {"mave": {"url": "https://…", "added_by": "…", "added_at": "…"}}
    }

Der Bot legt ihn beim Lesen über seinen eigenen Stand. Ein Wunsch ist
idempotent und muss nicht quittiert werden — ein doppelt verarbeiteter Eintrag
richtet also keinen Schaden an. Echte Aufträge (Produkt aufnehmen, Login
prüfen) brauchen einen Browser und kommen später dazu.

Der Bot schreibt hier NICHT hinein. Diese Datei gehört dem Worker.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)

_GH_API = "https://api.github.com/gists"
COMMANDS_FILE = "commands.json"

_TRUE = ("on", "an", "true", "yes", "y", "ja", "1")


def load_commands(token: str, gist_id: str) -> dict:
    """`commands.json` lesen. {} bei leer, fehlend oder Fehler.

    Ein Fehler darf den Lauf nicht anhalten: Ohne die Datei verhält sich der Bot
    exakt wie vorher, nur eben ohne die per Discord gesetzten Wünsche. Das ist
    die richtige Rückfallebene — lieber der alte Stand als gar kein Lauf.
    """
    if not (token and gist_id):
        return {}
    try:
        r = requests.get(
            f"{_GH_API}/{gist_id}",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        r.raise_for_status()
        content = (r.json().get("files", {}).get(COMMANDS_FILE, {}) or {}).get("content", "")
        return json.loads(content) if content.strip() else {}
    except (requests.RequestException, ValueError) as e:
        log.warning("commands.json nicht lesbar (%s) — Wünsche werden ignoriert", e)
        return {}


def enabled_override(cmds: dict, name: str) -> Optional[bool]:
    """An/Aus für ein Konto, oder None wenn per Discord nichts gesetzt wurde."""
    raw = (cmds.get("enabled") or {}).get(name)
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in _TRUE


def tracking_entries(cmds: dict) -> dict:
    """Per Discord eingetragene Sendungen, im Format von `manual_tracking`."""
    out = {}
    for label, val in (cmds.get("tracking") or {}).items():
        if isinstance(val, str):
            out[label] = val
        elif isinstance(val, dict) and val.get("url"):
            # `added_by`/`added_at` sind reine Nachvollziehbarkeit und haben in
            # der Verfolgung nichts zu suchen — nur übernehmen, was hermes_watch
            # auch auswertet.
            entry = {"url": val["url"]}
            for key in ("ping_env", "ping", "owner"):
                if val.get(key):
                    entry[key] = val[key]
            out[label] = entry
    return out
