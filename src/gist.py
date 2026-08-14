"""Zugriff auf das private Gist — ein Abruf pro Lauf, nicht sieben.

Vorher holten `orders` und `commands` das Gist je für sich, und zwar jedes Mal
neu: `check_orders`, `check_shipments`, `run_scans` und `main` zusammen bis zu
sieben Mal denselben `GET /gists/{id}`. Der liefert aber **alle** Dateien auf
einmal — die Wiederholung brachte also nichts außer Wartezeit.

Hier liegt der Abruf einmal zentral, samt Zwischenspeicher für den laufenden
Prozess. Geschrieben wird weiterhin sofort und einzeln: Ein Lauf kann jederzeit
abbrechen, und dann soll das, was schon feststeht, auch im Gist stehen.

**Semantik unverändert:** `read()` gibt bei jedem Aufruf ein frisches Objekt
zurück (aus dem gespeicherten Text neu geparst). Änderungen eines Aufrufers
sickern also nicht zu einem anderen durch, solange sie nicht geschrieben
wurden — genau wie vorher bei getrennten Abrufen.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)

_API = "https://api.github.com/gists"

# Rohinhalte der Gist-Dateien für DIESEN Lauf: {dateiname: text}.
# `None` heißt „noch nicht geholt", ein leeres Dict „geholt, Gist ist leer".
_cache: Optional[dict[str, str]] = None


def reset() -> None:
    """Zwischenspeicher leeren. Einmal zu Beginn eines Laufs aufrufen."""
    global _cache
    _cache = None


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _fetch(token: str, gist_id: str) -> dict[str, str]:
    """Alle Dateien des Gists holen. Bei Fehlern leer — nie werfen.

    Der Fehler wird bewusst nicht weitergereicht: Ohne Gist verhält sich der Bot
    wie vorher, nur ohne Bestellstand und ohne die per Discord gesetzten
    Wünsche. Lieber ein eingeschränkter Lauf als gar keiner.
    """
    global _cache
    if _cache is not None:
        return _cache

    try:
        r = requests.get(f"{_API}/{gist_id}", headers=_headers(token), timeout=15)
        r.raise_for_status()
        dateien = r.json().get("files") or {}
        _cache = {name: (f or {}).get("content", "") for name, f in dateien.items()}
    except (requests.RequestException, ValueError) as e:
        log.error("Gist-Load fehlgeschlagen: %s", e)
        _cache = {}
    return _cache


def read(token: str, gist_id: str, dateiname: str) -> dict:
    """Eine Datei als JSON. `{}` wenn sie fehlt, leer oder kaputt ist."""
    if not (token and gist_id):
        return {}
    inhalt = _fetch(token, gist_id).get(dateiname, "")
    if not inhalt.strip():
        return {}
    try:
        return json.loads(inhalt)
    except ValueError as e:
        log.error("%s im Gist ist kein gültiges JSON: %s", dateiname, e)
        return {}


def write(token: str, gist_id: str, dateiname: str, daten: dict) -> bool:
    """Eine Datei schreiben. Der PATCH überträgt NUR sie, andere bleiben unberührt."""
    if not (token and gist_id):
        return False
    text = json.dumps(daten, indent=2, ensure_ascii=False)
    try:
        r = requests.patch(
            f"{_API}/{gist_id}",
            headers=_headers(token),
            json={"files": {dateiname: {"content": text}}},
            timeout=15,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Gist-Save fehlgeschlagen: %s", e)
        return False

    # Zwischenspeicher mitziehen, damit ein späterer `read` im selben Lauf den
    # neuen Stand sieht. Ohne das wäre der Cache genau der Fehler, den er
    # vermeiden soll.
    if _cache is not None:
        _cache[dateiname] = text
    return True
