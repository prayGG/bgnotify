"""Alle Discord-Embed-Builder + Anzeige-Helfer (Sparklines, Zeitangaben).

Hier wird ausschließlich GERENDERT — keine Scrapes, keine State-Mutationen.
Jeder Builder bekommt fertige Daten und gibt ein Discord-Embed-Dict zurück.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from .pricing import display_price, fmt_price_value, price_value
from .config import product_state_key, product_urls, variant_labels

log = logging.getLogger(__name__)

COLOR_IN_STOCK = 0x57F287    # Discord native green
COLOR_OUT      = 0x95A5A6    # Discord native gray
COLOR_WARN     = 0xFEE75C    # Discord native yellow
COLOR_BLURPLE  = 0x5865F2    # Discord native blurple (Stats / Deploy)


# --------------------------------------------------------------------------
# Anzeige-Helfer
# --------------------------------------------------------------------------
def _price_change_suffix(prev_price: str, current_price: str) -> str:
    if not prev_price or not current_price or prev_price == current_price:
        return ""
    return f"⠀·⠀_war {prev_price}_"


def _humanize_duration(seconds: float) -> str:
    """Compact human-readable duration: '12 s', '4 min', '3 h', '2 T 14 h', '3 Wo'."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h"
    days = hours // 24
    if days < 14:
        hours_rem = hours % 24
        if hours_rem and days < 7:
            return f"{days} T {hours_rem} h"
        return f"{days} T"
    return f"{days // 7} Wo"


def _humanize_ago(iso: str) -> str:
    if not iso:
        return ""
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return ""
    delta = (datetime.now(timezone.utc) - when).total_seconds()
    if delta < 60:
        return "gerade eben"
    return f"vor {_humanize_duration(delta)}"


# OOS-Phasen kürzer als das sind fast immer Scraper-/Outage-Artefakte: ein
# site-weiter Hänger liest ~1 Run lang OOS und „erholt" sich danach sofort
# wieder. Der Outage-Guard (siehe stock_watch.check_products) fängt nur
# site-weite Ausfälle ab — ein einzelner Hänger bei genau einem Produkt rutscht
# durch und würde sonst als 1h-Phantom-OOS den Schnitt verfälschen.
MIN_OOS_PERIOD_SECONDS = 90 * 60


def _median(values: list) -> float:
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def _oos_period_durations(periods: list) -> list:
    """Echte OOS-Dauern in Sekunden — Artefakt-Blips rausgefiltert."""
    out = []
    for p in periods or []:
        try:
            start = datetime.fromisoformat(p["start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(p["end"].replace("Z", "+00:00"))
        except (ValueError, KeyError, TypeError):
            continue
        secs = (end - start).total_seconds()
        if secs >= MIN_OOS_PERIOD_SECONDS:
            out.append(secs)
    return out


def _typical_oos_duration(periods: list) -> tuple:
    """Median der echten OOS-Phasen + deren Anzahl. Median statt Mittelwert,
    damit ein einzelner Ausreißer (oder ein durchgerutschtes Artefakt) die
    Zahl nicht kippt. Gibt ("—", 0) zurück, wenn keine echte Phase übrig ist."""
    durations = _oos_period_durations(periods)
    if not durations:
        return "—", 0
    return _humanize_duration(_median(durations)), len(durations)


_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list) -> str:
    if not values or len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return _SPARK_CHARS[3] * len(values)
    span = hi - lo
    step = len(_SPARK_CHARS) - 1
    return "".join(_SPARK_CHARS[min(int((v - lo) / span * step), step)] for v in values)


def _has_last_known_state(s: dict) -> bool:
    return bool(s.get("price")) or bool(s.get("in_stock")) or bool(s.get("out_since"))


def _dashboard_sort_key(s: dict) -> int:
    if s.get("error") and not _has_last_known_state(s):
        return 2
    if not s.get("found") and not s.get("error"):
        return 2
    return 0 if s["in_stock"] else 1


# --------------------------------------------------------------------------
# Dashboard (persistente Status-Message, wird in place editiert)
# --------------------------------------------------------------------------
def build_dashboard_embed(
    statuses: list[dict], usd_eur: Optional[float] = None, labels: Optional[dict] = None
) -> dict:
    labels = labels or {}
    blocks: list[str] = []
    for s in sorted(statuses, key=_dashboard_sort_key):
        link = s.get("deep_link") or s.get("product_url", "")
        klick = f"⠀·⠀[Klick]({link})" if link else ""
        uncertain = s.get("error") and _has_last_known_state(s)

        if s.get("error") and not _has_last_known_state(s):
            sub = "⚠️⠀check failed"
        elif not s["found"] and not s.get("error"):
            sub = "⚠️⠀nicht gefunden"
        elif s["in_stock"]:
            shown_price = display_price(s.get("price", ""), usd_eur)
            shown_prev = display_price(s.get("previous_price", ""), usd_eur)
            price = f"⠀·⠀**{shown_price}**" if shown_price else ""
            delta = _price_change_suffix(shown_prev, shown_price)
            warn = "⠀·⠀⚠️_check unsicher_" if uncertain else ""
            sub = f"🟢⠀in stock{price}{delta}{warn}"
        else:
            shown_last = display_price(s.get("price", ""), usd_eur)
            last_suffix = f"⠀·⠀_zuletzt {shown_last}_" if shown_last else ""
            warn = "⠀·⠀⚠️_check unsicher_" if uncertain else ""
            sub = f"🔴⠀out of stock{last_suffix}{warn}"

        disp = labels.get(s["variant"], s["variant"])
        blocks.append(f"**{disp}**\n└⠀{sub}{klick}")

    any_in_stock = any(s["in_stock"] for s in statuses if s.get("found") or _has_last_known_state(s))
    any_hard_error = any(s.get("error") and not _has_last_known_state(s) for s in statuses)
    color = COLOR_WARN if any_hard_error and not any_in_stock else (COLOR_IN_STOCK if any_in_stock else COLOR_OUT)

    return {
        "author": {"name": "✦⠀⠀bgnotify · status⠀⠀✦"},
        "color": color,
        "description": "\n\n".join(blocks) if blocks else "_keine Produkte konfiguriert_",
        "footer": {"text": "Letzter Check"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------
# Stats-Karte (persistente Message, wird in place editiert)
# --------------------------------------------------------------------------
def build_stats_embed(cfg: dict, state: dict, usd_eur: Optional[float] = None) -> dict:
    """Persistent stats card — edited in place each run. Pin manually once."""
    bot_stats = state.get("bot_stats", {})
    products_state = state.get("products", {})
    labels = variant_labels(cfg)

    checks = bot_stats.get("total_checks", 0)
    restocks = bot_stats.get("total_restocks", 0)
    bot_sub = [
        f"Checks gesamt: **{checks:,}**".replace(",", " "),
        f"Restocks erkannt: **{restocks}**",
    ]
    first_iso = bot_stats.get("first_check_at", "")
    if first_iso:
        try:
            when = datetime.fromisoformat(first_iso.replace("Z", "+00:00"))
            delta = (datetime.now(timezone.utc) - when).total_seconds()
            duration = _humanize_duration(delta) if delta >= 60 else "<1 min"
            bot_sub.append(f"Aktiv seit: **{duration}**")
        except (ValueError, TypeError):
            pass
    bot_lines = ["🤖⠀**Bot**"]
    for i, line in enumerate(bot_sub):
        prefix = "└⠀" if i == len(bot_sub) - 1 else "├⠀"
        bot_lines.append(prefix + line)
    blocks = ["\n".join(bot_lines)]

    entries: list[tuple[str, str, dict]] = []
    for product in cfg.get("products") or []:
        urls = product_urls(product)
        if not urls:
            continue
        watch = product.get("watch_variants") or []
        emoji = product.get("emoji") or "💊"
        product_data = products_state.get(product_state_key(product, urls), {})
        for variant in watch:
            e = product_data.get(variant)
            if not e:
                continue
            entries.append((emoji, variant, e))

    # PS-Spiele in dieselbe Stats-Schleife einreihen — gleiche Entry-Form,
    # daher identisches Rendering (Sparkline, tief/hoch, OOS-Dauer, Restocks).
    ps_state = state.get("playstation", {})
    for game in (cfg.get("playstation") or {}).get("games") or []:
        url = game.get("url")
        if not url:
            continue
        e = ps_state.get(url)
        if not e:
            continue
        emoji = game.get("emoji") or "🎮"
        label = e.get("name") or game.get("name") or url
        entries.append((emoji, label, e))

    entries.sort(key=lambda t: 0 if t[2].get("in_stock") else 1)

    for emoji, variant, e in entries:
        sample_price = e.get("price", "") or e.get("lowest_price", "")
        lines = [f"{emoji}⠀**{labels.get(variant, variant)}**"]

        history = e.get("price_history", [])
        spark = _sparkline(history)
        if spark:
            first_str = fmt_price_value(history[0], sample_price, usd_eur)
            last_str = fmt_price_value(history[-1], sample_price, usd_eur)
            trend = f"**{first_str} → {last_str}**" if first_str != last_str else f"**{first_str}**"
            lines.append(f"├⠀📈⠀`{spark}`⠀{trend}")
        elif history:
            only_str = fmt_price_value(history[0], sample_price, usd_eur)
            lines.append(f"├⠀📈⠀**{only_str}**")

        oos_typical, oos_n = _typical_oos_duration(e.get("oos_periods", []))
        if oos_n:
            lines.append(f"├⠀⏱⠀**OOS-Dauer Ø: {oos_typical}**⠀·⠀{oos_n}×")
        else:
            lines.append("├⠀⏱⠀OOS-Dauer Ø: —")

        low = display_price(e.get("lowest_price", ""), usd_eur)
        high = display_price(e.get("highest_price", ""), usd_eur)
        low_ago = _humanize_ago(e.get("lowest_price_at", ""))
        high_ago = _humanize_ago(e.get("highest_price_at", ""))
        if low:
            lines.append(f"├⠀tief {low}" + (f" ({low_ago})" if low_ago else ""))
        if high:
            lines.append(f"├⠀hoch {high}" + (f" ({high_ago})" if high_ago else ""))

        rc = e.get("restock_count", 0)
        last_restock = _humanize_ago(e.get("last_restock_at", ""))
        if rc:
            rline = f"└⠀🔄⠀**{rc} Restocks**"
            if last_restock:
                rline += f"⠀·⠀letzter {last_restock}"
            lines.append(rline)
        else:
            lines.append("└⠀🔄⠀noch keine Restocks erkannt")

        blocks.append("\n".join(lines))

    return {
        "author": {"name": "✦⠀⠀bgnotify · stats⠀⠀✦"},
        "color": COLOR_BLURPLE,
        "description": "\n\n".join(blocks),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------
# Stock-Alerts (Restock + Out-of-stock)
# --------------------------------------------------------------------------
def build_restock_embed(restock: dict, usd_eur: Optional[float] = None, labels: Optional[dict] = None) -> dict:
    shown = display_price(restock.get("price", ""), usd_eur)
    price_line = f"### ⠀{shown}\n\n" if shown else ""
    return {
        "author": {"name": "✦⠀⠀restocked⠀⠀✦"},
        "title": (labels or {}).get(restock["variant"], restock["variant"]),
        "description": (
            price_line
            + f"**[→⠀⠀Jetzt bestellen]({restock['deep_link']})**"
        ),
        "color": COLOR_IN_STOCK,
        "footer": {"text": "bgpharmadrugs.to"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_oos_embed(item: dict, usd_eur: Optional[float] = None, labels: Optional[dict] = None) -> dict:
    """Embed for an in-stock -> out-of-stock transition. Quieter than the
    restock alert — gray accent, last seen price, no order button, no pings."""
    last = display_price(item.get("last_price", ""), usd_eur)
    last_line = f"_zuletzt {last}_\n\n" if last else ""
    link = item.get("deep_link") or item.get("product_url") or ""
    link_line = f"[→⠀⠀Produktseite]({link})" if link else ""
    return {
        "author": {"name": "✦⠀⠀out of stock⠀⠀✦"},
        "title": (labels or {}).get(item["variant"], item["variant"]),
        "description": last_line + link_line,
        "color": COLOR_OUT,
        "footer": {"text": "bgpharmadrugs.to"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------
# PlayStation-Preissenkung
# --------------------------------------------------------------------------
def build_ps_drop_embed(drop: dict) -> dict:
    """Embed für eine PS-Preissenkung — grüner Akzent, neuer Preis groß,
    alter Preis + Rabatt als Kontext, Link in den Store."""
    new_s = drop.get("new_price") or ""
    old_s = drop.get("old_price") or ""
    disc = drop.get("discount_text") or ""
    disc_suffix = f"⠀·⠀**{disc}**" if disc else ""
    lines = f"### ⠀{new_s}\n_war {old_s}_{disc_suffix}" if old_s else f"### ⠀{new_s}"
    return {
        "author": {"name": "✦⠀⠀preis gesenkt⠀⠀✦"},
        "title": drop.get("name") or "PlayStation",
        "description": lines + f"\n\n**[→⠀⠀Im PS Store ansehen]({drop['url']})**",
        "color": COLOR_IN_STOCK,
        "footer": {"text": "store.playstation.com"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------
# Forum-Post
# --------------------------------------------------------------------------
_ISO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def build_forum_embed(post: dict) -> dict:
    """Embed for one new BG forum post — green accent, minimal chrome."""
    excerpt = post.get("excerpt") or ""
    if len(excerpt) > 600:
        excerpt = excerpt[:597].rstrip() + "…"
    embed = {
        "author": {"name": "✦⠀⠀neuer post⠀⠀✦"},
        "title": post.get("thread_title") or "(ohne Titel)",
        "url": post.get("url", ""),
        "description": excerpt or "_(kein Snippet)_",
        "color": COLOR_IN_STOCK,
        "footer": {"text": "thinksteroids.com"},
    }
    posted_at = post.get("posted_at") or ""
    if _ISO_PREFIX.match(posted_at):
        embed["timestamp"] = posted_at
    return embed


# --------------------------------------------------------------------------
# Bestellstatus + Tracking
# --------------------------------------------------------------------------
_ORDER_STATUS_EMOJI = {
    "pending": "🕓", "processing": "📦", "preparing": "📦", "on-hold": "⏸️",
    "completed": "✅", "cancelled": "❌", "refunded": "↩️", "failed": "⚠️",
}
# Farbreise entlang des Ablaufs: grau → blau → türkis(Tracking) → grün.
# Seiten-/Negativzustände mit klar abgegrenzten Warnfarben.
_ORDER_STATUS_COLOR = {
    "pending":    0x95A5A6,  # grau      — wartet auf Zahlung (Start)
    "processing": 0x3498DB,  # blau      — in Bearbeitung (BG: "Preparing")
    "preparing":  0x3498DB,  # blau
    "on-hold":    0xE67E22,  # orange    — hängt / Klärung
    "completed":  0x57F287,  # grün      — fertig / angekommen (Ziel)
    "cancelled":  0xED4245,  # rot       — storniert
    "failed":     0x992D22,  # dunkelrot — fehlgeschlagen
    "refunded":   0x9B59B6,  # lila      — erstattet (Geld zurück)
}
# Tracking/„unterwegs" — türkis, sitzt visuell zwischen blau (Bearbeitung) und
# grün (angekommen). Bewusst NICHT grün, damit es sich von Completed abhebt.
_ORDER_TRACKING_COLOR = 0x1ABC9C


def _items_block(items: Optional[list[str]]) -> str:
    """Artikelzeilen für die Embed-Beschreibung (leer wenn keine bekannt)."""
    if not items:
        return ""
    return "\n\n" + "\n".join(f"·⠀{it}" for it in items)


def _order_link(url: Optional[str]) -> str:
    """Klickbarer Link zur Bestellseite (leer wenn keine URL)."""
    return f"\n\n[→⠀Bestellung ansehen]({url})" if url else ""


def build_order_status_embed(order: dict, fresh: bool = False, items: Optional[list[str]] = None) -> dict:
    slug = order.get("status", "")
    emoji = _ORDER_STATUS_EMOJI.get(slug, "📦")
    label = order.get("status_text") or slug or "—"
    head = "🆕⠀Neue Bestellung" if fresh else "Status-Update"
    return {
        "author": {"name": "✦⠀⠀bestellung⠀⠀✦"},
        "title": f"#{order.get('order_id', '')}",
        "description": f"{head}\n{emoji}⠀**{label}**" + _items_block(items) + _order_link(order.get("url")),
        "color": _ORDER_STATUS_COLOR.get(slug, COLOR_WARN),
        "footer": {"text": "bgpharmadrugs.to"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_order_tracking_embed(order_id: str, links: list[str], items: Optional[list[str]] = None,
                               url: Optional[str] = None) -> dict:
    body = "\n".join(f"[→⠀Sendung verfolgen]({l})" for l in links)
    return {
        "author": {"name": "✦⠀⠀tracking⠀⠀✦"},
        "title": f"#{order_id}",
        "description": f"🚚⠀**Tracking ist da**\n{body}" + _items_block(items) + _order_link(url) + "\n\n_Details in deiner Mail_",
        "color": _ORDER_TRACKING_COLOR,
        "footer": {"text": "via Hermes"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
