"""Orchestrator: Watcher laufen lassen, Discord aktualisieren, State speichern.

Ablauf pro Run (GitHub Actions, `python -m src.main`):

1. Shop-Produkte checken (`stock_watch`) → Statuses + Restock-/OOS-Transitions
2. Deploy-Announcement, wenn HEAD sich bewegt hat (`deploy`)
3. Neue BG-Forum-Posts (`forum_watch`, intervall-gegated)
3b. Einzelartikel ausserhalb des Shops (`retail_watch`) — wieder verfuegbar?
4. Bestellstatus + Tracking (`order_watch`, Stand im privaten Gist)
5. Dashboard + Stats-Karte in place editieren, Alerts posten (`notify`)
6. Fehler-Report in den Updates-Channel, wenn der Run Errors hatte (`health`)
7. state.json speichern (der Workflow committet sie zurück auf main)

Die Module dahinter:
- Scraper (reines Fetch+Parse): `bgpharma`, `forum`, `orders`
- Watcher (State + Transitions):  `stock_watch`, `forum_watch`, `order_watch`
- Darstellung (Embeds):           `embeds`
- Discord-Versand:                `notify`
- Fehler-Report:                  `health`
- Config/State + Preise:          `config`, `pricing`
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import sys

from . import commands, gist, health, notify
from .config import (
    load_config,
    load_ping_role_ids,
    load_ping_user_ids,
    load_state,
    save_state,
    variant_labels,
)
from .deploy import announce_deploy
from .embeds import (
    build_dashboard_embed,
    dashboard_group_names,
    dashboard_variants,
    build_forum_embed,
    build_oos_embed,
    build_retail_restock_embed,
    build_restock_embed,
    build_stats_embed,
)
from .forum_watch import check_forum
from .retail_watch import check_items
from .hermes_watch import check_shipments
from .order_watch import check_orders
from .pricing import fetch_usd_eur_rate
from .product_watch import merge_products, run_scans
from .stock_watch import check_products

log = logging.getLogger(__name__)


def _webhook_from_cfg(cfg: dict, key: str, default_env: str) -> str:
    return os.environ.get(cfg.get(key, default_env), "")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    errors = health.install()  # sammelt ab hier jeden log.error des Runs
    cfg = load_config()
    state = load_state()

    webhook = _webhook_from_cfg(cfg, "discord_webhook_env", "DISCORD_WEBHOOK_URL")
    if not webhook:
        log.warning("env var %s is empty — Discord disabled", cfg.get("discord_webhook_env", "DISCORD_WEBHOOK_URL"))
    updates_webhook = _webhook_from_cfg(cfg, "discord_updates_webhook_env", "DISCORD_UPDATES_WEBHOOK_URL")
    forum_webhook = _webhook_from_cfg(cfg, "discord_forum_webhook_env", "DISCORD_FORUM_WEBHOOK_URL")
    order_webhook = _webhook_from_cfg(cfg, "discord_order_webhook_env", "DISCORD_ORDER_WEBHOOK_URL")
    # Stock-alerts (restock + OOS) ideally go to the dedicated bg-notify
    # channel. Fall back to the main webhook so restock alerts don't go silent
    # if the new secret hasn't been configured yet.
    stock_webhook = _webhook_from_cfg(cfg, "discord_stock_webhook_env", "DISCORD_STOCK_WEBHOOK_URL") or webhook

    user_ids = load_ping_user_ids(cfg)
    role_ids = load_ping_role_ids(cfg)

    try:
        # Gist-Zwischenspeicher für diesen Lauf leeren. Ein Abruf liefert alle
        # Dateien auf einmal und wird bis zum Ende des Laufs wiederverwendet —
        # ohne Reset könnte ein langlebiger Prozess (Tests, lokales
        # Ausprobieren) einen alten Stand weiterreichen.
        gist.reset()

        # 0 — Per Command aufgenommene Produkte dazunehmen. Muss VOR dem
        # Stock-Check passieren, sonst wird das frisch aufgenommene Produkt
        # einen ganzen Lauf lang übersehen. Fällt der Gist aus, bleibt es bei
        # dem, was in config.yml steht — der Lauf läuft trotzdem.
        gist_cmds = commands.load_commands(
            os.environ.get("GIST_TOKEN", ""), os.environ.get("GIST_ID", "")
        )
        cfg["products"] = merge_products(cfg, gist_cmds)

        # ERST JETZT die Anzeige-Aliase einsammeln. Vor dem Merge kannte diese
        # Liste nur die aus `config.yml` — die per `/product rename` gesetzten
        # fehlten, und das Dashboard zeigte weiter den langen Originalwortlaut.
        # Nicht aufgefallen ist es, weil die Stats-Karte sich ihre Aliase selbst
        # aus `cfg` zieht (embeds.py) und deshalb immer richtig lag: zwei Karten
        # nebeneinander, eine mit dem neuen Namen, eine mit dem alten.
        labels = variant_labels(cfg)
        # Per `/product rename` gesetzte Namen legen sich DARÜBER. Sie sind die
        # jüngere Aussage, und nur so erreicht das Umbenennen auch die fest in
        # `config.yml` gepflegten Produkte — sonst wären ausgerechnet Roaccutane
        # und Tretinoin die einzigen, die man nicht umbenennen kann.
        labels.update(commands.product_labels(gist_cmds))

        # 1 — Shop-Produkte. Den (unabhängigen) USD/EUR-Kurs holen wir parallel
        # in einem Thread: er trifft einen anderen Host als der Shop, also
        # überlappt seine Latenz mit dem Scrapen statt sie davorzuhängen.
        # fetch_usd_eur_rate fängt intern alle Fehler ab (→ None), result()
        # kann also nicht werfen.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            rate_future = pool.submit(fetch_usd_eur_rate)
            statuses, restocks, oos_alerts = check_products(cfg, state)
            usd_eur = rate_future.result()
        log.info("USD->EUR rate: %s", usd_eur)

        # 2 — Deploy-Announcement
        announce_deploy(state, updates_webhook)

        # 3 — Forum-Posts
        new_forum_posts = check_forum(cfg, state)
        if new_forum_posts and forum_webhook:
            for post in new_forum_posts:
                notify.send_forum_post(forum_webhook, build_forum_embed(post))
        elif new_forum_posts:
            log.info("forum: %d new post(s) but forum webhook is empty", len(new_forum_posts))

        # 3b — Einzelartikel ausserhalb des Shops. Ein GET pro Stueck, also
        # billig genug fuer jeden Lauf — und bei einem Drop zaehlt jede Minute.
        # Landet im Restock-Channel: Fuer den, der es dort sieht, ist es
        # dasselbe Ereignis wie ein Restock im Shop.
        retail_statuses, retail_restocks = check_items(cfg, state)
        # Mit aufs Status-Board: Auch wenn nichts passiert, will man sehen, DASS
        # beobachtet wird — sonst ist ein stiller Watcher nicht von einem
        # kaputten zu unterscheiden.
        statuses.extend(retail_statuses)
        for r in retail_restocks:
            notify.send_restock_alert(
                stock_webhook, build_retail_restock_embed(r), user_ids, role_ids,
            )

        # 4 — Bestellstatus (eigener Webhook, eigener Gist-Stand)
        check_orders(cfg, order_webhook, user_ids, role_ids)

        # 4b — Hermes-Sendungen verfolgen. Läuft bewusst NACH check_orders: was
        # dort gerade an Tracking-Links gefunden wurde, steht im Gist unter
        # `auto_tracking` und wird noch im selben Run mitgenommen. Dazu die von
        # Hand eingetragenen (`manual_tracking`) ohne hinterlegtes Kundenkonto.
        check_shipments(cfg, order_webhook, user_ids, role_ids)

        # 4c — Offene Produkt-Auftraege aus Discord (`/product add`). Nach den
        # Bestellungen, weil ein Einlesen nur ein Seitenabruf ist und nichts
        # blockiert; das Ergebnis kommt als eigene Karte in den Channel.
        run_scans(cfg)

        # 5 — Discord aktualisieren. Dashboard + Stats sind persistente Karten im
        # Status-Channel und brauchen daher den Haupt-Webhook. Restock-/OOS-
        # Alerts gehen unabhängig davon in den Stock-Channel — deshalb NICHT an
        # `webhook` gekoppelt (sonst feuern sie nicht, wenn nur der Stock-Webhook
        # gesetzt ist). send_* sind no-ops bei leerer URL.
        # Von Hand gesetzte Reihenfolge (`/product move`). Zweitschlüssel: Die
        # Verfügbarkeit sortiert weiterhin zuerst, das hier entscheidet nur
        # innerhalb einer Stufe.
        order = commands.product_order(gist_cmds)
        # Die Überschriften veröffentlichen, damit `/product move` im
        # Autocomplete genau das anbietet, was im Dashboard steht.
        state["dashboard_names"] = dashboard_group_names(statuses)
        # Und die einzelnen Zeilen mit ihrem aktuellen Namen — daraus baut
        # `/product rename` sein Autocomplete.
        state["dashboard_variants"] = dashboard_variants(statuses, labels)

        if webhook:
            new_stats_id = notify.edit_in_place(
                webhook,
                build_stats_embed(cfg, state, usd_eur=usd_eur, order=order, labels=labels),
                message_id=state.get("stats_message_id", ""),
            )
            if new_stats_id:
                state["stats_message_id"] = new_stats_id

            new_id = notify.edit_in_place(
                webhook,
                build_dashboard_embed(statuses, usd_eur=usd_eur, labels=labels, order=order),
                message_id=state.get("dashboard_message_id", ""),
            )
            if new_id:
                state["dashboard_message_id"] = new_id

        for r in restocks:
            notify.send_restock_alert(
                stock_webhook, build_restock_embed(r, usd_eur=usd_eur, labels=labels), user_ids, role_ids,
            )
        for o in oos_alerts:
            notify.send_oos_alert(stock_webhook, build_oos_embed(o, usd_eur=usd_eur, labels=labels))
    except Exception as e:
        # Harter Crash: erst melden + State sichern, dann den Run trotzdem
        # fehlschlagen lassen, damit der Workflow rot wird.
        log.exception("run crashed: %s: %s", type(e).__name__, e)
        health.report(state, updates_webhook, errors)
        save_state(state)
        raise

    # 6 — Fehler-Report (nur wenn sich das Fehlerbild geändert hat)
    health.report(state, updates_webhook, errors)

    # 7 — State persistieren
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
