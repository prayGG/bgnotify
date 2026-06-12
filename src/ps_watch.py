"""PlayStation-Store-Preis-Watcher.

Führt den State der konfigurierten PS-Spiele GENAU wie die Shop-Produkte
(gleiche Entry-Form in state.json), damit Dashboard und Stats-Karte sie
identisch rendern. Liefert zusätzlich Preissenkungen für den Ping-Channel.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import playstation
from .pricing import fmt_price_cents, price_value

log = logging.getLogger(__name__)


def check_playstation(cfg: dict, state: dict) -> tuple[list[dict], list[dict]]:
    """Konfigurierte PS-Store-Spiele prüfen; State + Status genau wie die Shop-
    Produkte führen, damit sie im Dashboard UND der Stats-Karte identisch
    gerendert werden (in/out of stock, Preis, „war X", Sparkline, tief/hoch,
    OOS-Dauer, Restocks).

    Gibt (statuses, drops) zurück:
      - statuses: Dashboard-Form (gleiche Keys wie check_products) → angehängt
        an die Produkt-Statuses, fließt in build_dashboard_embed.
      - drops: Preissenkungen für den Ping-Channel (eigenes Embed).

    „in stock" = kaufbar (ADD_TO_CART-Preis vorhanden). Digitale Spiele sind
    quasi immer grün; rot nur wenn delisted / nur via Abo. Erstkontakt setzt
    nur die Baseline (kein Ping). State unter state["playstation"][url] in der
    GLEICHEN Entry-Form wie ein Produkt-Variant (price="€ X.XX", price_history
    als Euro-Floats, lowest/highest, out_since, oos_periods, restock_count …).
    """
    ps_state = state.setdefault("playstation", {})
    statuses: list[dict] = []
    drops: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    seen_urls: set[str] = set()
    for game in (cfg.get("playstation") or {}).get("games") or []:
        url = game.get("url")
        if not url:
            continue
        seen_urls.add(url)
        cfg_name = game.get("name") or ""
        entry = ps_state.setdefault(url, {})
        disp_name = cfg_name or entry.get("name") or url

        try:
            info = playstation.check(url, cfg_name)
        except Exception as e:
            log.error("playstation check failed for %s: %s", url, e)
            info = None

        # Scrape fehlgeschlagen — letzten bekannten Stand zeigen, KEINE
        # Transition/Drop (sonst falscher Alarm beim nächsten Erfolg).
        if info is None or not info.get("found"):
            statuses.append({
                "product_name": disp_name, "product_url": url, "variant": disp_name,
                "in_stock": bool(entry.get("in_stock")),
                "price": entry.get("price", ""), "previous_price": entry.get("previous_price", ""),
                "out_since": entry.get("out_since", ""), "found": False,
                "deep_link": url, "error": True,
            })
            continue

        entry["name"] = info["name"] or cfg_name
        disp_name = entry["name"] or disp_name
        in_stock_now = info["price_value"] is not None  # kaufbar = „in stock"
        in_stock_prev = entry.get("in_stock")
        new_price = fmt_price_cents(info["price_value"], info["currency"]) if in_stock_now else ""
        prev_price = entry.get("price", "")
        out_since_before = entry.get("out_since")
        entry["discount_text"] = info["discount_text"]

        # OOS-Bookkeeping (out_since stempeln / beim Wieder-kaufbar löschen).
        if not in_stock_now and not entry.get("out_since"):
            entry["out_since"] = now_iso
        elif in_stock_now:
            entry.pop("out_since", None)

        # Preisänderung merken (für „war X").
        if in_stock_now and new_price and prev_price and new_price != prev_price:
            entry["previous_price"] = prev_price

        # tief/hoch + Preis-History (numerisch, dedupliziert, letzte 30) — wie Produkte.
        new_val = price_value(new_price)
        if new_val is not None:
            low_val = price_value(entry.get("lowest_price", ""))
            if low_val is None or new_val < low_val:
                entry["lowest_price"] = new_price
                entry["lowest_price_at"] = now_iso
            high_val = price_value(entry.get("highest_price", ""))
            if high_val is None or new_val > high_val:
                entry["highest_price"] = new_price
                entry["highest_price_at"] = now_iso
            history = entry.setdefault("price_history", [])
            if not history or history[-1] != new_val:
                history.append(new_val)
                if len(history) > 30:
                    entry["price_history"] = history[-30:]

        statuses.append({
            "product_name": disp_name, "product_url": url, "variant": disp_name,
            "in_stock": in_stock_now,
            "price": new_price or entry.get("price", ""),
            "previous_price": entry.get("previous_price", ""),
            "out_since": entry.get("out_since", ""),
            "found": True, "deep_link": url, "error": False,
        })

        # Transitions. Restock (out→in) wie bei Produkten; zusätzlich der
        # Preissenkungs-Ping (unabhängig vom Restock — feuert auch, wenn das
        # Spiel durchgehend kaufbar war und nur billiger wurde).
        prev_val = price_value(prev_price)
        if in_stock_prev is None:
            log.info("ps baseline: %s %s", disp_name, "in stock" if in_stock_now else "out")
        elif in_stock_now and not in_stock_prev:
            entry["restock_count"] = entry.get("restock_count", 0) + 1
            entry["last_restock_at"] = now_iso
            if out_since_before:
                periods = entry.setdefault("oos_periods", [])
                periods.append({"start": out_since_before, "end": now_iso})
                if len(periods) > 20:
                    entry["oos_periods"] = periods[-20:]

        if in_stock_now and prev_val is not None and new_val is not None and new_val < prev_val:
            log.info("ps price drop: %s %s -> %s", disp_name, prev_price, new_price)
            drops.append({
                "name": disp_name, "url": url,
                "old_price": prev_price, "new_price": new_price,
                "discount_text": info["discount_text"],
            })

        entry["in_stock"] = in_stock_now
        if in_stock_now and new_price:
            entry["price"] = new_price
        entry["found"] = True
        entry["last_check_at"] = now_iso

    # Stand für entfernte Spiele aufräumen, damit state.json nicht wächst.
    for url in list(ps_state.keys()):
        if url not in seen_urls:
            log.info("removing stale ps state entry: %s", url)
            del ps_state[url]

    return statuses, drops
