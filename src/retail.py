"""Verfügbarkeit beliebiger Produktseiten — über die schema.org-Daten der Seite.

Warum nicht wieder ein eigener Scraper pro Shop: Fast jeder Shop legt seine
Produktdaten als JSON-LD in die Seite, weil Google sie sonst nicht als
Rich Result anzeigt. Genau dieselben Felder, die dort für die Suchmaschine
stehen, beantworten auch unsere Frage — Name, Preis, Verfügbarkeit. Ein Parser
deckt damit alle Shops ab, die das tun, statt einen pro Händler.

Nachgemessen an crocs.eu, und der Unterschied ist eindeutig:

    verfügbar    "offers": {"availability": "https://schema.org/InStock",
                            "price": 74.99, "priceCurrency": "EUR"}
    ausverkauft  "offers": {}          ← kein availability, kein Preis

Die Wörter „Sold Out" und „Notify Me" stehen dagegen auf JEDER Seite im
Textbundle, egal wie der Zustand ist. Wer danach sucht, misst die Übersetzung
statt den Bestand — deshalb wird hier ausschließlich das JSON-LD ausgewertet.

Kein Browser nötig: ein GET, ein Parser. Damit ist der Check billig genug, um
ihn in jedem Lauf zu machen.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Ein Browser-UA, weil manche Shops nackte Clients aussperren. Keine Tarnung,
# nur die Mindestangabe, mit der man überhaupt eine Seite bekommt.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_LD_BLOCK = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)

# Zustände, in denen man das Ding wirklich kaufen kann. `PreOrder` und
# `BackOrder` bewusst NICHT: Beides heißt „bestellbar, aber nicht da", und ein
# Alarm dafür wäre bei einem Restock-Melder ein Fehlalarm.
_KAUFBAR = ("instock", "limitedavailability", "onlineonly", "instoreonly")


def _bloecke(html: str) -> list:
    """Alle JSON-LD-Objekte der Seite, flach ausgepackt.

    Drei Verschachtelungen kommen in freier Wildbahn vor und werden hier alle
    aufgelöst: ein einzelnes Objekt, eine Liste von Objekten, und `@graph`.
    """
    out = []
    for roh in _LD_BLOCK.findall(html):
        try:
            daten = json.loads(roh.strip())
        except ValueError:
            continue  # kaputtes JSON-LD ist verbreitet — einfach überspringen
        for eintrag in (daten if isinstance(daten, list) else [daten]):
            if not isinstance(eintrag, dict):
                continue
            graph = eintrag.get("@graph")
            out.extend(g for g in graph if isinstance(g, dict)) if isinstance(graph, list) else out.append(eintrag)
    return out


def _ist_produkt(eintrag: dict) -> bool:
    typ = eintrag.get("@type")
    if isinstance(typ, list):
        return any(str(t).lower() == "product" for t in typ)
    return str(typ).lower() == "product"


def _angebot(produkt: dict) -> dict:
    """Das Angebot aus einem Product-Eintrag — auch wenn es mehrere sind.

    Bei einer Liste gewinnt das erste KAUFBARE. Sonst zählte bei einem Shop mit
    einem Angebot pro Größe zufällig die erste Größe, und ein Restock in Größe
    43 bliebe unbemerkt, weil 36 noch ausverkauft ist.
    """
    o = produkt.get("offers") or {}
    if isinstance(o, dict) and str(o.get("@type", "")).lower() == "aggregateoffer":
        inner = o.get("offers")
        if isinstance(inner, list) and inner:
            o = inner
    if isinstance(o, list):
        angebote = [x for x in o if isinstance(x, dict)]
        for x in angebote:
            if _kaufbar(x):
                return x
        return angebote[0] if angebote else {}
    return o if isinstance(o, dict) else {}


def _kaufbar(angebot: dict) -> bool:
    a = str(angebot.get("availability") or "").lower()
    return any(z in a for z in _KAUFBAR)


def parse(html: str) -> dict:
    """Produktdaten aus dem HTML. `found=False`, wenn kein Product drinsteht."""
    for eintrag in _bloecke(html):
        if not _ist_produkt(eintrag):
            continue
        angebot = _angebot(eintrag)
        preis = angebot.get("price")
        return {
            "found": True,
            "name": str(eintrag.get("name") or "").strip(),
            "sku": str(eintrag.get("sku") or "").strip(),
            "in_stock": _kaufbar(angebot),
            "price": "" if preis in (None, "") else str(preis),
            "currency": str(angebot.get("priceCurrency") or "").strip(),
        }
    return {"found": False, "name": "", "sku": "", "in_stock": False, "price": "", "currency": ""}


def check(url: str, timeout: int = 20) -> dict:
    """Eine Produktseite abfragen. Wirft nie — Fehler kommen als `error` zurück.

    Ein Fehlschlag darf NICHT als „ausverkauft" durchgehen: Sonst löst der
    nächste geglückte Abruf einen Restock-Alarm aus, obwohl sich nichts geändert
    hat. Der Aufrufer erkennt das an `error` und lässt den Stand dann in Ruhe.
    """
    try:
        r = requests.get(url, headers={"User-Agent": _UA,
                                       "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"},
                         timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as e:
        return {"url": url, "found": False, "in_stock": False, "price": "",
                "currency": "", "name": "", "sku": "", "error": str(e)[:200]}

    daten = parse(r.text)
    daten["url"] = url
    daten["error"] = ""
    if not daten["found"]:
        # Kein Product im JSON-LD: Entweder liefert der Shop keins, oder wir
        # sind auf einer Blockade-/Fehlerseite gelandet. Beides ist „unbekannt",
        # nicht „ausverkauft" — deshalb als Fehler behandeln.
        daten["error"] = "keine schema.org-Produktdaten auf der Seite"
    return daten


if __name__ == "__main__":  # Debug: python -m src.retail <url>
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(json.dumps(check(sys.argv[1]), indent=2, ensure_ascii=False))
