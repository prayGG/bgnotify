"""Produkt-Aufträge aus Discord abarbeiten.

Der erste Fall, in dem ein Command nicht allein aus einem Wunschzustand
besteht: `/product add` kann der Worker nicht beantworten, weil er die Seite
nicht kennt — welche Varianten es gibt, weiß erst, wer sie geladen hat.

Also ein echter **Auftrag**:

    /product add <link>   ->  commands.json: scans[url]
    Bot-Lauf              ->  Seite lesen, Varianten merken, Karte posten
                          ->  Ergebnis in order-state.json: product_scans[url]
    /product add <link> <variante>  ->  Autocomplete kennt die Varianten jetzt

Auch hier ohne Quittung über die Dateigrenze hinweg: Der Auftrag bleibt in
`commands.json` stehen (die gehört dem Worker), erledigt ist er, sobald ein
Ergebnis im Stand des Bots liegt. Dasselbe Muster wie bei der Login-Prüfung.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import bgpharma, commands, notify, orders
from .embeds import build_product_scan_embed

log = logging.getLogger(__name__)

# Pro Lauf höchstens so viele Seiten einlesen. Ein Scan ist ein normaler
# Seitenabruf, aber mehrere auf einmal wären ein auffälliger Burst — und eilig
# ist es nicht, der nächste Lauf kommt in zehn Minuten.
_MAX_SCANS_PER_RUN = 2


def merge_products(cfg: dict, cmds: dict) -> list:
    """Produkte aus `config.yml` + die per Command aufgenommenen.

    Warum getrennt: Die `config.yml` ist voller Erklärkommentare — jede Zeile
    begründet, warum ein Match-String roh bleibt oder ein Intervall so gewählt
    ist. Ein Programm, das YAML einliest und neu schreibt, wirft die alle weg.
    Von Hand gepflegtes bleibt deshalb in der Datei, per Command Hinzugefügtes
    kommt aus dem Gist, und zusammengeführt wird erst beim Laden.
    """
    aus_config = list(cfg.get("products") or [])
    bekannt = {u for p in aus_config for u in (p.get("urls") or [p.get("url")]) if u}

    for p in commands.command_products(cmds):
        # Gleiche URL schon in der Config? Dann gewinnt die Config — sie ist von
        # Hand gepflegt und kennt womöglich Aliase, die ein Command nicht setzt.
        if p["url"] in bekannt:
            log.info("products: '%s' steht schon in config.yml — Gist-Eintrag ignoriert", p["name"])
            continue
        aus_config.append(p)
    return aus_config


def run_scans(cfg: dict, webhook: str) -> None:
    """Offene Einlese-Aufträge abarbeiten. Fehler brechen nie den Lauf ab.

    `webhook` zeigt auf den Bot-Channel: Das Ergebnis ist die Antwort auf einen
    selbst getippten Command, keine Meldung. Deshalb ohne Ping und nicht im
    Bestell-Channel.
    """
    import os

    token = os.environ.get("GIST_TOKEN", "")
    gist_id = os.environ.get("GIST_ID", "")
    if not (token and gist_id):
        return

    cmds = commands.load_commands(token, gist_id)
    auftraege = list((cmds.get("scans") or {}).keys())
    if not auftraege:
        return

    st = orders.load_order_state(token, gist_id)
    ergebnisse = st.setdefault("product_scans", {})

    offen = [u for u in auftraege if u not in ergebnisse][:_MAX_SCANS_PER_RUN]
    if not offen:
        return

    for url in offen:
        try:
            daten = bgpharma.list_variants(url)
        except Exception as e:
            # Auch das Scheitern festhalten: Sonst versucht es jeder Lauf erneut,
            # und wer den Link eingetippt hat, wartet vergeblich auf Antwort.
            log.error("products: '%s' nicht lesbar: %s", url, e)
            daten = {"title": "", "simple": False, "variants": [], "error": str(e)[:200]}

        daten["scanned_at"] = datetime.now(timezone.utc).isoformat()
        ergebnisse[url] = daten

        if webhook:
            notify.send_command_result(webhook, build_product_scan_embed(url, daten))
        log.info("products: '%s' eingelesen (%d Variante(n))", url, len(daten.get("variants") or []))

    orders.save_order_state(token, gist_id, st)
