"""Watcher für Produktseiten außerhalb des Shops — meldet, wenn etwas zurück ist.

Gedacht für einzelne, gehypte Artikel, die monatelang ausverkauft sind und dann
ohne Ankündigung wieder auftauchen. Der Unterschied zum Shop-Watcher: Dort geht
es um Preise und Verlauf, hier um genau einen Moment — aus „weg" wird „da".

Zustand pro URL in `state.json` (öffentlich, deshalb steht hier nichts
Persönliches drin — nur was auf der Produktseite ohnehin für alle sichtbar ist):

    retail: {"<url>": {"in_stock": bool, "price": "74.99", "currency": "EUR",
                       "name": "…", "last_check_at": "…"}}
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import retail

log = logging.getLogger(__name__)


def check_items(cfg: dict, state: dict) -> tuple[list[dict], list[dict]]:
    """(Statuses, Restocks) für alle konfigurierten Artikel.

    Ein Artikel, den wir zum ersten Mal sehen, wird STILL aufgenommen — auch
    wenn er gerade verfügbar ist. Sonst gäbe es beim Hinzufügen sofort einen
    Alarm für etwas, das man gerade selbst eingetragen hat, und beim nächsten
    Artikel wieder. Gemeldet wird erst ein echter Wechsel.
    """
    items = (cfg.get("retail") or {}).get("items") or []
    if not items:
        return [], []

    zustand = state.setdefault("retail", {})
    statuses: list[dict] = []
    restocks: list[dict] = []

    for item in items:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        name = item.get("name") or ""
        daten = retail.check(url)

        vorher = zustand.get(url)
        eintrag = dict(vorher or {})

        if daten["error"]:
            # Nicht erreichbar heißt NICHT ausverkauft. Würde der Fehler als
            # „weg" gespeichert, löste der nächste geglückte Abruf einen
            # Restock-Alarm aus, obwohl sich nichts geändert hat.
            log.warning("retail: '%s' nicht abrufbar: %s", name or url, daten["error"])
            eintrag["last_error"] = daten["error"][:200]
            zustand[url] = eintrag
            statuses.append({"name": name, "url": url, "error": daten["error"],
                             "in_stock": bool((vorher or {}).get("in_stock"))})
            continue

        jetzt_da = bool(daten["in_stock"])
        anzeige = name or daten["name"] or url

        if vorher is None:
            log.info("retail: '%s' aufgenommen (%s)", anzeige,
                     "verfügbar" if jetzt_da else "nicht verfügbar")
        elif jetzt_da and not vorher.get("in_stock"):
            restocks.append({
                "name": anzeige,
                "url": url,
                "price": daten["price"],
                "currency": daten["currency"],
                "emoji": item.get("emoji") or "",
            })
            log.info("retail: '%s' ist WIEDER DA (%s %s)", anzeige,
                     daten["price"], daten["currency"])
        elif vorher.get("in_stock") and not jetzt_da:
            # Bewusst nur ins Log: Dass etwas wieder weg ist, ist keine
            # Nachricht, für die es sich zu pingen lohnt.
            log.info("retail: '%s' ist wieder ausverkauft", anzeige)

        zustand[url] = {
            "in_stock": jetzt_da,
            "price": daten["price"],
            "currency": daten["currency"],
            "name": daten["name"] or name,
            "last_check_at": datetime.now(timezone.utc).isoformat(),
        }
        statuses.append({"name": anzeige, "url": url, "in_stock": jetzt_da,
                         "price": daten["price"], "currency": daten["currency"],
                         "error": ""})

    return statuses, restocks
