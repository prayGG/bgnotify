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


def _has_last_known_state(s: dict) -> bool:
    return bool(s.get("price")) or bool(s.get("in_stock")) or bool(s.get("out_since"))


def _stock_sort_key(s: dict) -> int:
    """Sortier-Reihenfolge: 🟢 in stock oben (0), ⚠️ Fehler/nicht gefunden in der
    Mitte (1), 🔴 out of stock ganz unten (2). Innerhalb gleicher Stufe greift
    der stabile Sort → Config-Reihenfolge. Funktioniert auf Dashboard-Status-
    Dicts wie auch auf state.json-Einträgen (Stats)."""
    if s.get("error") and not _has_last_known_state(s):
        return 1
    if not s.get("found", True) and not s.get("error"):
        return 1
    return 0 if s.get("in_stock") else 2


def _short_label(product_name: str, alias: str) -> str:
    """Anzeige-Alias ohne den vorangestellten Produktnamen — für Varianten, die
    unter einem Gruppen-Header (`Tretinoin`) hängen, damit der Name nicht doppelt
    steht (`Tretinoin 0.05% 30g` → `0.05% 30g`). Lässt unpassende Aliase roh."""
    if product_name and alias.lower().startswith(product_name.lower()):
        rest = alias[len(product_name):].lstrip(" -·–—").strip()
        return rest or alias
    return alias


def _dashboard_dot_segs(s: dict, usd_eur: Optional[float]) -> tuple[str, list[str], bool]:
    """(dot, segmente, is_error) für eine Variante. Der Status steckt allein im
    Dot (🟢/🔴) — kein 'in stock'/'out of stock'-Text mehr, der Punkt ist
    eindeutig genug und hält die Zeile kurz. Bei hartem Fehler/not-found ist
    `is_error` True und segmente enthält den Hinweistext (Dot = ⚠️)."""
    uncertain = s.get("error") and _has_last_known_state(s)
    if s.get("error") and not _has_last_known_state(s):
        return "⚠️", ["check failed"], True
    if not s["found"] and not s.get("error"):
        return "⚠️", ["nicht gefunden"], True

    segs: list[str] = []
    if s["in_stock"]:
        dot = "🟢"
        shown_price = display_price(s.get("price", ""), usd_eur)
        shown_prev = display_price(s.get("previous_price", ""), usd_eur)
        if shown_price:
            segs.append(f"**{shown_price}**")
        if shown_prev and shown_price and shown_prev != shown_price:
            segs.append(f"_last {shown_prev}_")
    else:
        dot = "🔴"
        shown_last = display_price(s.get("price", ""), usd_eur)
        if shown_last:
            segs.append(f"_last {shown_last}_")
    if uncertain:
        segs.append("⚠️_check unsicher_")
    return dot, segs, False


def _linked(text: str, link: str) -> str:
    """Macht `text` zum Hyperlink auf `link` (bold-fett). Ohne Link nur fett —
    so ist der Produkt-/Variantenname selbst klickbar, ein extra '· Klick'
    entfällt und die Zeile bleibt kurz."""
    return f"**[{text}]({link})**" if link else f"**{text}**"


def _dashboard_status_line(s: dict, usd_eur: Optional[float]) -> str:
    """Statuszeile EINES Einzelprodukts (verlinkter Titel steht separat darüber)."""
    dot, segs, _is_error = _dashboard_dot_segs(s, usd_eur)
    detail = "⠀·⠀".join(segs)
    return f"{dot}⠀{detail}" if detail else dot


def _dashboard_group_row(tree: str, short: str, s: dict, usd_eur: Optional[float]) -> str:
    """Varianten-Zeile innerhalb einer Produkt-Gruppe: Dot direkt nach dem
    Baum-Zeichen (├/└), dann der verlinkte Variantenname."""
    link = s.get("deep_link") or s.get("product_url", "")
    dot, segs, _is_error = _dashboard_dot_segs(s, usd_eur)
    segs = [_linked(short, link)] + segs
    return f"{tree}⠀{dot}⠀" + "⠀·⠀".join(segs)


# --------------------------------------------------------------------------
# Dashboard (persistente Status-Message, wird in place editiert)
# --------------------------------------------------------------------------
def group_name(status: dict) -> str:
    """Überschrift, unter der eine Variante im Dashboard hängt.

    Zugleich der Schlüssel, unter dem `/product move` eine Position merkt —
    deshalb an einer Stelle definiert statt an dreien abgeschrieben.
    """
    return status.get("product_name") or status["variant"]


def dashboard_variants(statuses: list[dict], labels: Optional[dict] = None) -> list[dict]:
    """Jede Zeile des Dashboards: `{key, label}`.

    `key` ist der Varianten-String, mit dem gegen die Seite abgeglichen wird —
    der bleibt beim Umbenennen unangetastet. `label` ist, wie die Zeile GERADE
    heißt. Genau dieses Paar braucht `/product rename`: anbieten, was man sieht,
    speichern unter dem, was sich nicht ändert.

    Der Bot legt die Liste in `state.json` ab, damit der Worker sie im
    Autocomplete anbieten kann, ohne `config.yml` nachzubauen.
    """
    labels = labels or {}
    out: list[dict] = []
    gesehen = set()
    for s in statuses:
        key = s.get("variant") or ""
        if not key or key in gesehen:
            continue
        gesehen.add(key)
        out.append({"key": key, "label": labels.get(key, key)})
    return out


def dashboard_group_names(statuses: list[dict]) -> list[str]:
    """Die Überschriften des Dashboards, ohne Dopplungen, in Anzeige-Reihenfolge.

    Der Bot legt sie in `state.json` ab, damit der Worker im Autocomplete von
    `/product move` genau das anbieten kann, was auch wirklich dasteht. Sonst
    müsste er `config.yml` nachbauen und liefe bei jeder Änderung auseinander.
    """
    out: list[str] = []
    for s in statuses:
        n = group_name(s)
        if n not in out:
            out.append(n)
    return out


# Position für alles, was nicht von Hand einsortiert wurde. Mittig gewählt,
# damit man ohne Umnummerieren sowohl darüber als auch darunter Platz hat.
DEFAULT_POSITION = 100


def _order_key(name: str, order: Optional[dict]) -> tuple:
    """Zweitschlüssel der Sortierung: von Hand gesetzte Position, dann Name.

    Die Verfügbarkeit bleibt der ERSTE Schlüssel — grün gehört nach oben, das
    ist die Frage, die man beim Draufschauen stellt. Die Handsortierung
    entscheidet nur innerhalb einer Stufe.
    """
    try:
        pos = int((order or {}).get(name, DEFAULT_POSITION))
    except (TypeError, ValueError):
        pos = DEFAULT_POSITION
    return (pos, name.lower())


def build_dashboard_embed(
    statuses: list[dict], usd_eur: Optional[float] = None, labels: Optional[dict] = None,
    order: Optional[dict] = None,
) -> dict:
    labels = labels or {}

    # Varianten desselben Produkts zusammenfassen (erste-Sichtung-Reihenfolge),
    # damit z.B. beide Tretinoin-Optionen als EIN Block erscheinen statt als
    # zwei lose Einträge. Einzelvarianten-Produkte bleiben optisch unverändert.
    groups: dict[str, list[dict]] = {}
    for s in statuses:
        groups.setdefault(group_name(s), []).append(s)

    # Gruppen nach bestem Mitglied sortieren (in-stock zuerst, OOS ganz unten),
    # innerhalb einer Stufe nach der von Hand gesetzten Position.
    ordered = sorted(
        groups.items(),
        key=lambda kv: (min(_stock_sort_key(m) for m in kv[1]), _order_key(kv[0], order)),
    )

    blocks: list[str] = []
    for name, members in ordered:
        members = sorted(members, key=_stock_sort_key)
        if len(members) == 1:
            s = members[0]
            disp = labels.get(s["variant"], s["variant"])
            link = s.get("deep_link") or s.get("product_url", "")
            blocks.append(f"{_linked(disp, link)}\n└⠀{_dashboard_status_line(s, usd_eur)}")
            continue

        rows = [f"**{name}**"]
        for i, s in enumerate(members):
            tree = "└" if i == len(members) - 1 else "├"
            short = _short_label(name, labels.get(s["variant"], s["variant"]))
            rows.append(_dashboard_group_row(tree, short, s, usd_eur))
        blocks.append("\n".join(rows))

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
def _stats_body_lines(e: dict, usd_eur: Optional[float]) -> list[str]:
    """Die Detailzeilen EINER Variante (Sparkline, OOS-Dauer, tief/hoch,
    Restocks) — ohne Titelzeile, damit sie unter einem Einzel- wie auch unter
    einem Gruppen-Header hängen können."""
    sample_price = e.get("price", "") or e.get("lowest_price", "")
    lines: list[str] = []

    history = e.get("price_history", [])
    if history:
        first_str = fmt_price_value(history[0], sample_price, usd_eur)
        last_str = fmt_price_value(history[-1], sample_price, usd_eur)
        if first_str != last_str:
            trend = f"**{first_str} → {last_str}**"
            icon = "📈" if history[-1] > history[0] else "📉"
        else:
            trend = f"**{first_str}**"
            icon = "📊"
        lines.append(f"├⠀{icon}⠀{trend}")

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
    return lines


def build_stats_embed(cfg: dict, state: dict, usd_eur: Optional[float] = None,
                      order: Optional[dict] = None, labels: Optional[dict] = None) -> dict:
    """Persistent stats card — edited in place each run. Pin manually once."""
    bot_stats = state.get("bot_stats", {})
    products_state = state.get("products", {})
    # Aliase kommen von aussen — GENAU wie beim Dashboard. Holte sich diese
    # Karte sie weiter selbst aus `cfg`, kaeme sie ohne die per `/product rename`
    # gesetzten Namen aus, und die beiden Karten im selben Channel zeigten
    # wieder verschiedene Namen fuer dasselbe Produkt. Genau so ist es schon
    # einmal passiert, nur andersherum.
    labels = labels if labels is not None else variant_labels(cfg)

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

    # Produkte gruppieren: jede Gruppe = (emoji, name, [(variant, entry), …]).
    # Ein Produkt mit mehreren Varianten (z.B. Tretinoin) bleibt so als eine
    # Einheit zusammen statt in lose Einzelkarten zu zerfallen.
    groups: list[tuple[str, str, list[tuple[str, dict]]]] = []
    for product in cfg.get("products") or []:
        urls = product_urls(product)
        if not urls:
            continue
        watch = product.get("watch_variants") or []
        emoji = product.get("emoji") or "💊"
        name = product.get("name") or urls[0]
        product_data = products_state.get(product_state_key(product, urls), {})
        members = [(v, product_data[v]) for v in watch if product_data.get(v)]
        if members:
            members.sort(key=lambda ve: _stock_sort_key(ve[1]))
            groups.append((emoji, name, members))

    # Gleicher Schlüssel wie das Dashboard — beide Karten stehen untereinander
    # im selben Channel, unterschiedliche Reihenfolgen wären nur verwirrend.
    groups.sort(key=lambda g: (min(_stock_sort_key(e) for _v, e in g[2]), _order_key(g[1], order)))

    for emoji, name, members in groups:
        if len(members) == 1:
            variant, e = members[0]
            head = f"{emoji}⠀**{labels.get(variant, variant)}**"
            blocks.append("\n".join([head] + _stats_body_lines(e, usd_eur)))
            continue

        # Mehrere Varianten: ein Gruppen-Header (Emoji + Produktname), darunter
        # je Variante direkt der Unterblock — ohne Leerzeilen dazwischen, damit
        # die Gruppe optisch eine Einheit bleibt (das fette Label trennt genug).
        lines = [f"{emoji}⠀**{name}**"]
        for variant, e in members:
            short = _short_label(name, labels.get(variant, variant))
            lines.append(f"**{short}**")
            lines += _stats_body_lines(e, usd_eur)
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
    last_line = f"_last {last}_\n\n" if last else ""
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
# Der Shop schreibt seine WooCommerce-Slugs auf Englisch raus ("Completed") —
# gemeint ist dabei der Stand IM SHOP, nicht der des Pakets. Genau da entsteht
# die Verwirrung: "Completed" heißt abgeschickt, nicht angekommen. Deshalb steht
# unter jedem Status eine Zeile Klartext.
_ORDER_STATUS_HINT = {
    "pending":    "wartet auf Zahlung",
    "processing": "bezahlt, wird bearbeitet",
    "preparing":  "wird gepackt",
    "on-hold":    "hängt — BG klärt etwas",
    "completed":  "bei BG fertig & rausgeschickt — noch nicht zugestellt",
    "cancelled":  "storniert",
    "refunded":   "erstattet",
    "failed":     "fehlgeschlagen",
}


def _items_block(items: Optional[list[str]]) -> str:
    """Artikelzeilen für die Embed-Beschreibung (leer wenn keine bekannt)."""
    if not items:
        return ""
    return "\n\n" + "\n".join(f"·⠀{it}" for it in items)


def _order_link(url: Optional[str]) -> str:
    """Klickbarer Link zur Bestellseite (leer wenn keine URL)."""
    return f"\n\n**[→⠀⠀Bestellung ansehen]({url})**" if url else ""


def _titled(owner: Optional[str], order_id: str) -> str:
    """Titel jeder Bestell-, Tracking- und Sendungskarte: nur die Konto-Bezeichnung.

    Oben steht schlicht der Kontoname — dieselbe Zeile, egal ob die Karte aus
    dem Kundenkonto kommt oder aus einer von Hand eingetragenen Sendung. Die
    Bestellnummer taucht bewusst NIRGENDS auf: Konten ohne hinterlegte BG-Zugänge
    (nur über `manual_tracking` verfolgt) haben nie eine, dann stünde sie mal da
    und mal nicht. Wer die Nummer braucht, klickt "Bestellung ansehen".

    Ohne `owner` — Konto ohne `label` in der config — bleibt die Nummer der
    einzige Anker, sonst hätte die Karte gar keinen Titel.
    """
    return owner if owner else f"#{order_id}"


def build_order_status_embed(order: dict, fresh: bool = False, items: Optional[list[str]] = None,
                             owner: Optional[str] = None) -> dict:
    slug = order.get("status", "")
    emoji = _ORDER_STATUS_EMOJI.get(slug, "📦")
    label = order.get("status_text") or slug or "—"
    head = "🆕⠀Neue Bestellung" if fresh else "Status-Update"
    hint = _ORDER_STATUS_HINT.get(slug, "")
    status_block = f"{emoji}⠀**{label}**" + (f"\n_{hint}_" if hint else "")
    return {
        "author": {"name": "✦⠀⠀bestellung⠀⠀✦"},
        "title": _titled(owner, str(order.get('order_id', ''))),
        "description": f"{head}\n{status_block}" + _items_block(items) + _order_link(order.get("url")),
        "color": _ORDER_STATUS_COLOR.get(slug, COLOR_WARN),
        "footer": {"text": "bgpharmadrugs.to"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


_MAX_EVENTS_IN_EMBED = 12


def build_shipment_embed(label: str, data: dict, new_events: list[dict], url: str,
                         first: bool = False, owner: Optional[str] = None,
                         order_id: str = "") -> dict:
    """Statusmeldung zu einer verfolgten Sendung.

    Zeigt **jedes neue Ereignis mit Zeitstempel** — beim ersten Mal den ganzen
    bisherigen Verlauf, danach nur noch, was seit der letzten Meldung dazukam.
    Ist die Sendung zugestellt, kommen die Zustelldetails (Datum/Uhrzeit/Ort) mit.

    Gehört die Sendung zu einer Bestellung (`owner` bekannt), steht oben dasselbe
    wie auf deren Bestell- und Tracking-Karten. Von Hand eingetragene
    Sendungen haben kein Konto — die behalten ihr im Gist frei gewähltes Label,
    das dann genauso schlicht der Kontoname sein sollte.
    """
    head = "🚚⠀**Sendung wird verfolgt**" if first else "📍⠀**Neuer Sendungsstatus**"

    shown = new_events[-_MAX_EVENTS_IN_EMBED:]
    more = len(new_events) - len(shown)
    parts = [head]
    if more > 0:
        parts.append(f"_… {more} ältere Ereignisse ausgelassen_")
    for e in shown:
        when = " · ".join(x for x in (e.get("date", ""), e.get("time", "")) if x)
        parts.append(f"\n**{when}**\n{e.get('text', '')}")

    # Zustelldetails nur wenn vorhanden (erscheinen erst nach der Zustellung).
    det = data.get("details") or {}
    keep = [(k, v) for k, v in det.items()
            if k.lower().startswith(("zugestellt", "uhrzeit", "zustellort", "abgeholt"))]
    if keep:
        parts.append("\n" + "\n".join(f"`{k}:` {v}" for k, v in keep))

    parts.append(f"\n**[→⠀⠀Sendung verfolgen]({url})**")

    # Zu einer Bestellung gehörend → gleicher Titel wie deren Karten.
    # Von Hand eingetragen → das im Gist frei gewählte Label.
    title = _titled(owner, str(order_id)) if (owner or order_id) else label
    return {
        "author": {"name": "✦⠀⠀sendung⠀⠀✦"},
        "title": title,
        "description": "\n".join(parts)[:4000],
        "color": _ORDER_TRACKING_COLOR,
        "footer": {"text": "via Hermes"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_order_tracking_embed(order_id: str, links: list[str], items: Optional[list[str]] = None,
                               url: Optional[str] = None, owner: Optional[str] = None) -> dict:
    body = "\n".join(f"**[→⠀⠀Sendung verfolgen]({l})**" for l in links)
    return {
        "author": {"name": "✦⠀⠀tracking⠀⠀✦"},
        "title": _titled(owner, order_id),
        "description": f"🚚⠀**Tracking ist da**\n{body}" + _items_block(items) + _order_link(url)
                       + "\n\n_Der Verlauf kommt ab jetzt automatisch — jede Station als eigene Meldung._",
        "color": _ORDER_TRACKING_COLOR,
        "footer": {"text": "via Hermes"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------
# Fehler-Report (Updates-Channel)
# --------------------------------------------------------------------------
COLOR_ERROR = 0xED4245  # Discord native red

_MAX_ERROR_LINES = 15


def build_error_embed(messages: list[str]) -> dict:
    """Embed für die gesammelten ERROR-Logs eines Runs — eine Zeile pro Fehler."""
    lines = [f"·⠀{m[:180]}" for m in messages[:_MAX_ERROR_LINES]]
    if len(messages) > _MAX_ERROR_LINES:
        lines.append(f"_…und {len(messages) - _MAX_ERROR_LINES} weitere_")
    return {
        "author": {"name": "✦⠀⠀fehler⠀⠀✦"},
        "description": "\n".join(lines)[:4000],
        "color": COLOR_ERROR,
        "footer": {"text": "bgnotify"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_recovery_embed() -> dict:
    """Entwarnung, wenn ein zuvor gemeldetes Fehlerbild wieder weg ist."""
    return {
        "author": {"name": "✦⠀⠀fehler⠀⠀✦"},
        "description": "✅⠀läuft wieder fehlerfrei",
        "color": COLOR_IN_STOCK,
        "footer": {"text": "bgnotify"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_account_idle_embed(label: str) -> dict:
    """Konto hat sich nach der Zustellung selbst abgeschaltet.

    Ohne diese Karte wäre das Abschalten unsichtbar — und beim nächsten Blick
    auf `/account list` stünde „aus", ohne dass jemand es ausgeschaltet hätte.
    Genau die Sorte Zustand, die man dem Bot dann nicht mehr glaubt.
    """
    return {
        "author": {"name": "✦⠀⠀konto⠀⠀✦"},
        "title": label,
        "description": "💤⠀**Alles zugestellt — Konto ruht wieder**\n\nDer Bot loggt sich "
                       "nicht mehr ein. Vor der nächsten Bestellung einmal "
                       "`/account enable`.",
        "color": COLOR_OUT,
        "footer": {"text": "bgpharmadrugs.to"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_account_check_embed(label: str, ok: bool, fehler: str = "") -> dict:
    """Ergebnis der ersten Login-Prüfung eines per `/account add` hinterlegten Kontos.

    Kommt genau einmal, direkt nach dem Anlegen. Ob die Zugangsdaten stimmen,
    kann erst ein echter Lauf sagen — der Command selbst hat keinen Browser und
    könnte es deshalb nicht prüfen.
    """
    if ok:
        return {
            "author": {"name": "✦⠀⠀konto⠀⠀✦"},
            "title": label,
            "description": "✅⠀**Login erfolgreich**\n\nDer Bot ist drin. Einschalten mit "
                           "`/account enable`, sobald du bestellt hast — nach der Zustellung "
                           "schaltet er von selbst wieder ab.",
            "color": COLOR_IN_STOCK,
            "footer": {"text": "bgpharmadrugs.to"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # Den Fehlertext knapp mitgeben: "Login nicht erfolgreich" und "Timeout" sind
    # zwei völlig verschiedene Aufgaben für den, der es gerade eingetippt hat.
    grund = re.sub(r"\s+", " ", fehler).strip()
    if len(grund) > 200:
        grund = grund[:200].rsplit(" ", 1)[0] + " …"
    return {
        "author": {"name": "✦⠀⠀konto⠀⠀✦"},
        "title": label,
        "description": "⚠️⠀**Login fehlgeschlagen**\n\nBenutzername oder Passwort stimmen nicht — oder BG hat "
                       "den Versuch abgewiesen.\n\nMit `/account remove` entfernen und `/account add` neu "
                       "versuchen."
                       + (f"\n\n_{grund}_" if grund else ""),
        "color": COLOR_WARN,
        "footer": {"text": "bgpharmadrugs.to"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_product_scan_embed(url: str, daten: dict) -> dict:
    """Was auf einer per `/product add` angemeldeten Seite gefunden wurde.

    Zeigt bewusst ALLE Varianten (bis zur Discord-Grenze): Wer aussucht, will
    die Auswahl sehen, nicht die ersten fünf und ein "und weitere".
    """
    if daten.get("error"):
        return {
            "author": {"name": "✦⠀⠀produkt⠀⠀✦"},
            "title": "Seite nicht lesbar",
            "description": f"[{url}]({url})\n\n⚠️⠀_{daten['error']}_\n\n"
                           "Stimmt der Link? Er muss auf eine Produktseite zeigen.",
            "color": COLOR_WARN,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    titel = daten.get("title") or "Produkt"
    if daten.get("simple"):
        beschreibung = (f"**[{titel}]({url})**\n\nEinzelprodukt — keine Varianten zur Auswahl.\n\n"
                        "Mit `/product add` erneut aufrufen, um es aufzunehmen.")
    else:
        varianten = daten.get("variants") or []
        liste = "\n".join(f"⠀·⠀{v}" for v in varianten)
        if len(liste) > 3500:
            liste = liste[:3500].rsplit("\n", 1)[0] + f"\n⠀·⠀… und {len(varianten)} insgesamt"
        beschreibung = (f"**[{titel}]({url})**\n\n**{len(varianten)}** Varianten gefunden:\n{liste}\n\n"
                        "Mit `/product add` und der gewünschten Variante aufnehmen — "
                        "die Auswahl geht jetzt per Autocomplete.")

    return {
        "author": {"name": "✦⠀⠀produkt⠀⠀✦"},
        "description": beschreibung,
        "color": COLOR_BLURPLE,
        "footer": {"text": "bgpharmadrugs.to"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
