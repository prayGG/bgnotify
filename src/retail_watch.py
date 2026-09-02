"""Watcher für Einzelartikel außerhalb des Shops — meldet, wenn etwas zurück ist.

Gedacht für gehypte Sachen, die monatelang ausverkauft sind und dann ohne
Ankündigung wieder auftauchen. Der Unterschied zum Shop-Watcher: Dort geht es um
Preise und Verlauf, hier um genau einen Moment — aus „weg" wird „da".

Ein Artikel hat MEHRERE Quellen, und das ist der Kern: Ein Restock erwischt
nicht überall gleichzeitig, und welcher Shop zuerst liefert, weiß man vorher
nicht. Gemeldet wird deshalb der Artikel, nicht die Seite — sobald irgendeine
Quelle ihn führt, kommt genau EINE Meldung, mit dem Link dorthin. Eine Karte pro
Shop wäre bei drei Quellen dreimal dieselbe Nachricht.

Zustand pro Artikel in `state.json` (öffentlich — hier steht nur, was auf der
Produktseite ohnehin für alle sichtbar ist):

    retail: {"<name>": {"in_stock": bool, "price": "74.99", "currency": "EUR",
                        "source": "<url der Quelle>", "last_check_at": "…"}}
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import retail

log = logging.getLogger(__name__)


def _quellen(item: dict) -> list[str]:
    """URLs eines Artikels — `urls:` als Liste, `url:` als Einzelfall."""
    roh = item.get("urls") or ([item["url"]] if item.get("url") else [])
    return [str(u).strip() for u in roh if str(u).strip()]


def check_items(cfg: dict, state: dict) -> tuple[list[dict], list[dict]]:
    """(Statuses, Restocks) für alle konfigurierten Artikel.

    Ein Artikel, den wir zum ersten Mal sehen, wird STILL aufgenommen — auch
    wenn er gerade verfügbar ist. Sonst gäbe es beim Eintragen sofort einen
    Alarm für etwas, das man gerade selbst hinzugefügt hat. Gemeldet wird nur
    ein echter Wechsel.
    """
    items = (cfg.get("retail") or {}).get("items") or []
    if not items:
        return [], []

    zustand = state.setdefault("retail", {})

    # Was nicht mehr in der Config steht, fliegt raus. `state.json` wird bei
    # jedem Lauf zurueckcommittet — ein Eintrag, den niemand mehr liest, bliebe
    # sonst fuer immer darin stehen. Betrifft auch Altbestaende aus der Zeit,
    # als hier noch nach URL statt nach Artikelname geschluesselt wurde.
    gewollt = {(i.get("name") or "").strip() for i in items}
    for tot in [k for k in zustand if k not in gewollt]:
        del zustand[tot]
        log.info("retail: '%s' steht nicht mehr in der Config — Rest entfernt", tot)

    statuses: list[dict] = []
    restocks: list[dict] = []

    for item in items:
        name = (item.get("name") or "").strip()
        quellen = _quellen(item)
        if not (name and quellen):
            continue

        treffer = None       # erste Quelle, die den Artikel führt
        fehler: list[str] = []
        gelesen = False      # mindestens eine Quelle war überhaupt lesbar

        for url in quellen:
            daten = retail.check(url)
            if daten["error"]:
                fehler.append(f"{url}: {daten['error']}")
                continue
            gelesen = True
            if daten["in_stock"]:
                treffer = daten
                break        # eine reicht — die restlichen Quellen sparen wir uns

        vorher = zustand.get(name) or {}

        if not gelesen:
            # KEINE Quelle war lesbar. Das ist „unbekannt", nicht „ausverkauft":
            # Würde es als „weg" gespeichert, meldete der nächste geglückte
            # Abruf einen Restock, obwohl sich nie etwas geändert hat.
            for f in fehler:
                log.warning("retail: %s", f)
            log.warning("retail: '%s' — keine Quelle lesbar, Stand bleibt", name)
            statuses.append({"name": name, "in_stock": bool(vorher.get("in_stock")),
                             "error": "; ".join(fehler)[:300]})
            continue

        # Einzelne kaputte Quellen sind kein Drama, solange eine geantwortet hat.
        for f in fehler:
            log.info("retail: %s", f)

        jetzt_da = treffer is not None

        if not vorher:
            log.info("retail: '%s' aufgenommen (%s)", name,
                     "verfügbar" if jetzt_da else "nicht verfügbar")
        elif jetzt_da and not vorher.get("in_stock"):
            restocks.append({
                "name": name,
                "url": treffer["url"],
                "price": treffer["price"],
                "currency": treffer["currency"],
                "emoji": item.get("emoji") or "",
            })
            log.info("retail: '%s' ist WIEDER DA — %s %s bei %s", name,
                     treffer["price"], treffer["currency"], treffer["url"])
        elif vorher.get("in_stock") and not jetzt_da:
            # Bewusst nur ins Log: Dass etwas wieder weg ist, ist keine
            # Nachricht, für die es sich zu pingen lohnt.
            log.info("retail: '%s' ist wieder ausverkauft", name)

        zustand[name] = {
            "in_stock": jetzt_da,
            "price": treffer["price"] if treffer else "",
            "currency": treffer["currency"] if treffer else "",
            "source": treffer["url"] if treffer else "",
            "last_check_at": datetime.now(timezone.utc).isoformat(),
        }
        statuses.append({"name": name, "in_stock": jetzt_da,
                         "price": treffer["price"] if treffer else "",
                         "currency": treffer["currency"] if treffer else "",
                         "error": ""})

    return statuses, restocks
