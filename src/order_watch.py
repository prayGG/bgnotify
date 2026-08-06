"""Bestellstatus-Watcher: BG-Kundenkonto → privater Discord-Channel.

Diffed den Bestellstand pro Konto gegen das private Gist (state.json ist
öffentlich → tabu für Order-Daten) und postet Status-Updates + Tracking.
Fetch/Parse liegt in `orders`, das Rendern in `embeds`.
"""
from __future__ import annotations

import logging
import os
import random
from datetime import datetime, timezone

from . import notify, orders
from .config import parse_ids
from .embeds import build_order_status_embed, build_order_tracking_embed

log = logging.getLogger(__name__)

# Status, in denen eine Tracking-Note auftauchen kann.
_TRACKABLE_STATUS = {"processing", "preparing", "on-hold", "completed"}
# Endzustände — eine Bestellung in einem dieser Status gilt als "nicht offen".
_TERMINAL_STATUS = {"completed", "cancelled", "refunded", "failed"}
# Nachrüstung: Bestellungen, deren Tracking-Karte schon RAUS ist, bevor es das
# automatische Verfolgen gab, holen ihren Link einmalig nach — aber nur, solange
# die Bestellung jung ist. Ältere Hermes-Links sind ohnehin tot, und ohne diese
# Grenze würde die halbe Bestellhistorie nochmal durch die Sendungsverfolgung
# laufen.
_BACKFILL_MAX_AGE_DAYS = 30


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("on", "an", "true", "yes", "y", "ja", "1")


def _order_enabled(st: dict, name: str) -> bool:
    """An/Aus-Schalter aus dem Gist-Feld `enabled` — pro Konto:

    - Dict (empfohlen): `{"a": "on", "b": "off"}`  → einfach on/off pro Konto
    - `true`                                        → alle Konten an
    - Liste `["a"]`                                 → nur diese Konten an
    - fehlt / `false`                               → aus (kein Login)

    So aktivierst du Tracking nur wenn du wirklich bestellt hast (Gist editieren,
    kein Code-Commit) — in Bestellpausen also NULL authentifizierte Logins.
    """
    en = st.get("enabled", False)
    if isinstance(en, dict):
        return _truthy(en.get(name, False))
    if en is True:
        return True
    if isinstance(en, list):
        return name in en
    return False


def _backfill_pending(acct: dict) -> bool:
    """Wartet hier noch eine Bestellung darauf, ihren Tracking-Link an die
    Sendungsverfolgung zu übergeben?

    Trifft auf alles zu, dessen Tracking-Karte raus ist, bevor es das
    automatische Verfolgen gab. Ob wirklich etwas nachgeholt wird, entscheidet
    danach `want_detail` über das Bestelldatum (`_recent`) — hier zählt nur, ob
    sich ein Login dafür überhaupt lohnt.
    """
    return any(o.get("tracking_posted") and not o.get("tracking_registered")
               for o in (acct.get("orders") or {}).values())


def _orders_due(acct: dict, interval_minutes: int, idle_interval_minutes: int) -> bool:
    """Ist dieser Account fällig? Zwei-Gang-Takt (offen→schnell, sonst Ruhe) mit
    ±10% Jitter. Kein last_check_at = noch nie geprüft = fällig."""
    # Steht die einmalige Nachrüstung noch aus, ist der Account SOFORT fällig.
    # Sonst bliebe eine laufende Sendung bis zum nächsten Idle-Takt (bis zu 24 h)
    # ungetrackt: die Bestellung ist ja "completed", also terminal, also greift
    # der Ruhe-Takt — genau dann, wenn das Paket unterwegs ist. `_backfill_done`
    # deckelt das auf GENAU einen Lauf; danach wird wieder normal gedrosselt,
    # auch wenn der Login gescheitert ist (kein Hämmern bei kaputtem Konto).
    if not acct.get("_backfill_done") and _backfill_pending(acct):
        return True
    has_open = any(v.get("status") not in _TERMINAL_STATUS for v in (acct.get("orders") or {}).values())
    effective = interval_minutes if has_open else idle_interval_minutes
    last = acct.get("last_check_at", "")
    if not last:
        return True
    try:
        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return True
    threshold = effective * 60 * random.uniform(0.9, 1.1)
    return (datetime.now(timezone.utc) - dt).total_seconds() >= threshold


def _recent(order: dict, days: int = _BACKFILL_MAX_AGE_DAYS) -> bool:
    """Bestellung jünger als `days`? Ohne lesbares Datum: nein (nichts nachrüsten)."""
    iso = order.get("date_iso") or ""
    if not iso:
        return False
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days <= days


def _register_tracking(auto: dict, order_id: str, links: list[str],
                       owner: str, ping_env: str) -> None:
    """Gefundene Tracking-Links in den Gist-Block `auto_tracking` schreiben.

    Damit übernimmt `hermes_watch` die Sendung ab dem nächsten Lauf von selbst:
    Verlauf scrapen, jedes neue Ereignis posten, bei Zustellung aufhören. Vorher
    endete die Kette bei der "Tracking ist da"-Karte — verfolgt wurden nur die
    von Hand ins Gist getippten Sendungen (`manual_tracking`).

    Label wie auf der Karte ("pray #37143"), damit Bestell- und Sendungskarten
    optisch zusammengehören. Discord-IDs landen NICHT im Gist — gespeichert wird
    nur der Name des Secrets, aufgelöst wird beim Posten.
    """
    base = f"{owner} #{order_id}" if owner else f"#{order_id}"
    for i, link in enumerate(links):
        label = base if i == 0 else f"{base} ({i + 1})"
        entry = {"url": link, "order_id": order_id}
        if owner:
            entry["owner"] = owner   # Kartentitel: "pray" — wie bei den Bestellkarten
        if ping_env:
            entry["ping_env"] = ping_env
        if auto.get(label) == entry:
            continue
        auto[label] = entry
        log.info("orders: '%s' zur Hermes-Verfolgung eingetragen", label)


def _check_one_account(name: str, webhook: str, user: str, pw: str,
                       ping_ids: list[str], role_ids: list[str], acct: dict,
                       owner: str = "", auto_tracking: dict | None = None,
                       ping_env: str = "") -> None:
    """Login + Diff + Post für GENAU einen Account; mutiert `acct` in place.

    last_check_at wird VOR dem Login gesetzt → Fehler-Drossel (ein gescheiterter
    Login löst trotzdem den Backoff aus, kein Hämmern). fetch() darf werfen —
    der Aufrufer fängt das und speichert den Stand (mit gesetztem last_check_at).
    """
    order_map = acct.setdefault("orders", {})
    initialized = acct.get("_initialized", False)
    acct["last_check_at"] = datetime.now(timezone.utc).isoformat()
    # Zusammen mit last_check_at VOR dem Login setzen: der Sofort-Lauf für die
    # Nachrüstung ist damit verbraucht, egal wie er ausgeht.
    acct["_backfill_done"] = True

    def want_detail(o: dict) -> bool:
        if not initialized:
            return False  # Baseline-Lauf: keine History nachladen
        prev = order_map.get(o["order_id"])
        if prev is None:
            return True  # neue Bestellung → Detailseite für die Artikel (+ ggf. Tracking)
        if o["status"] not in _TRACKABLE_STATUS:
            return False
        if not prev.get("tracking_posted"):
            return True
        # Karte ist raus, die Sendung hängt aber noch nicht in der Verfolgung
        # (Bestellung von vor dem Auto-Tracking) → Link einmalig nachholen.
        return not prev.get("tracking_registered") and _recent(o)

    olist, details, cookies = orders.fetch(user, pw, want_detail, cookies=acct.get("cookies"))
    acct["cookies"] = cookies  # Session für nächsten Lauf merken (privates Gist)

    if not initialized:
        for o in olist:
            done = o["status"] == "completed"
            order_map[o["order_id"]] = {"status": o["status"], "tracking_posted": done,
                                        "tracking_registered": done}
        acct["_initialized"] = True
        log.info("orders[%s]: Baseline gesetzt (%d Bestellungen), nichts gepostet", name, len(olist))
        return

    for o in olist:
        oid, slug = o["order_id"], o["status"]
        detail = orders.parse_order_detail(details[oid]) if oid in details else None
        items = (detail or {}).get("items") or None

        prev = order_map.get(oid)
        if prev is None:
            # Erstkontakt mit dieser Bestellung. Bereits abgeschlossene/stornierte
            # Altlasten werden nur still übernommen — sonst knallt bei frischem
            # oder von Hand geleertem State die ganze Bestellhistorie in den
            # Channel. `tracking_posted` gleich mitsetzen, damit auch deren
            # Tracking-Karte nicht nachkommt.
            stale = slug in _TERMINAL_STATUS
            order_map[oid] = {"status": slug, "tracking_posted": stale,
                              "tracking_registered": stale}
            prev = order_map[oid]
            if items:
                prev["items"] = items
            if stale:
                log.info("orders: #%s (%s) still übernommen — Altbestellung", oid, slug)
            else:
                notify.send_order_update(webhook, build_order_status_embed(o, fresh=True, items=prev.get("items"), owner=owner), ping_ids, role_ids)
        else:
            if items and not prev.get("items"):
                prev["items"] = items  # Artikel einmalig sichern
            if prev.get("status") != slug:
                notify.send_order_update(webhook, build_order_status_embed(o, items=prev.get("items"), owner=owner), ping_ids, role_ids)
                prev["status"] = slug

        was_posted = bool(prev.get("tracking_posted"))
        if detail and detail.get("tracking"):
            if not was_posted:
                notify.send_order_update(webhook, build_order_tracking_embed(oid, detail["tracking"], items=prev.get("items"), url=o.get("url"), owner=owner), ping_ids, role_ids)
                prev["tracking_posted"] = True
            # Ab hier übernimmt hermes_watch: Sendungsverlauf automatisch verfolgen.
            if auto_tracking is not None and not prev.get("tracking_registered"):
                _register_tracking(auto_tracking, oid, detail["tracking"], owner, ping_env)
                prev["tracking_registered"] = True
        elif detail and was_posted:
            # Nachrüst-Versuch, aber kein Link mehr auf der Seite — nichts zu
            # holen, also die Detailseite auch nicht in jedem Lauf neu laden.
            prev["tracking_registered"] = True


def check_orders(cfg: dict, default_webhook: str, default_ping_ids: list[str], role_ids: list[str]) -> None:
    """BG-Bestellungen aller konfigurierten Accounts → privater Discord-Channel.

    Stand pro Account im privaten Gist unter `accounts.<name>` (NICHT state.json
    — das ist öffentlich). Mehrere Accounts via `orders.accounts` (Credentials
    als Secrets, nur deren Env-Namen stehen in der Config).

    STAGGERING: pro Bot-Lauf wird höchstens EIN Account eingeloggt (der am
    längsten überfällige). So loggen sich nie zwei Accounts gleichzeitig von
    derselben IP ein — verhindert, dass BG die Konten korreliert.
    """
    token = os.environ.get("GIST_TOKEN", "")
    gist_id = os.environ.get("GIST_ID", "")
    if not (token and gist_id):
        log.info("orders: GIST_TOKEN/GIST_ID fehlen — übersprungen")
        return

    orders_cfg = cfg.get("orders") or {}
    interval = int(orders_cfg.get("check_interval_minutes", 240))
    idle = int(orders_cfg.get("idle_interval_minutes", 1440))
    cfg_accounts = orders_cfg.get("accounts") or [
        {"name": "default", "username_env": "BG_USERNAME", "password_env": "BG_PASSWORD"}
    ]

    st = orders.load_order_state(token, gist_id)
    # Migration: alter flacher Stand (Single-Account) → unter erstem Account-Namen.
    if "orders" in st and "accounts" not in st:
        st["accounts"] = {cfg_accounts[0]["name"]: {
            "orders": st.pop("orders"),
            "last_check_at": st.pop("last_check_at", ""),
            "_initialized": st.pop("_initialized", False),
        }}
    accounts_state = st.setdefault("accounts", {})

    # Beim ersten Mal die on/off-Schalter ins Gist schreiben, damit man sie nur
    # noch umschreiben muss (kein Tippen von Klammern/Struktur).
    if "enabled" not in st:
        st["enabled"] = {a["name"]: "off" for a in cfg_accounts}
        orders.save_order_state(token, gist_id, st)
        log.info("orders: enabled-Schalter im Gist angelegt (alle 'off') — bei Bestellung auf 'on' setzen")
        return

    # An/Aus-Schalter: nur aktivierte Konten (Gist-Feld `enabled`) → in
    # Bestellpausen keine Logins. Standard = aus.
    enabled_names = [a["name"] for a in cfg_accounts if _order_enabled(st, a["name"])]
    if not enabled_names:
        log.info("orders: alle Konten 'off' (Gist `enabled`) — übersprungen.")
        return

    # Nur aktivierte Accounts mit vorhandenen Secrets (Login + Ziel-Webhook).
    resolved = []
    for a in cfg_accounts:
        if a["name"] not in enabled_names:
            continue
        user = os.environ.get(a.get("username_env", "BG_USERNAME"), "")
        pw = os.environ.get(a.get("password_env", "BG_PASSWORD"), "")
        if not (user and pw):
            continue
        webhook = os.environ.get(a["webhook_env"], "") if a.get("webhook_env") else default_webhook
        if not webhook:
            continue
        ping = parse_ids(os.environ.get(a["ping_env"], "")) if a.get("ping_env") else default_ping_ids
        resolved.append({"name": a["name"], "user": user, "pw": pw, "webhook": webhook,
                         "ping": ping, "owner": a.get("label") or "",
                         "ping_env": a.get("ping_env") or ""})

    if not resolved:
        log.info("orders: aktiviert, aber Secrets/Webhook fehlen — übersprungen")
        return

    # Fällige Accounts sammeln, dann nur den am längsten überfälligen prüfen.
    due = [r for r in resolved if _orders_due(accounts_state.setdefault(r["name"], {}), interval, idle)]
    if not due:
        log.info("orders: nichts fällig (%d Account(s))", len(resolved))
        return
    due.sort(key=lambda r: accounts_state[r["name"]].get("last_check_at", ""))  # "" zuerst, dann ältester
    pick = due[0]

    acct = accounts_state[pick["name"]]
    # Gefundene Tracking-Links landen hier drin; `check_shipments` (läuft im
    # selben Run direkt danach) liest den Block und verfolgt die Sendung.
    auto_tracking = st.setdefault("auto_tracking", {})
    try:
        _check_one_account(pick["name"], pick["webhook"], pick["user"], pick["pw"],
                           pick["ping"], role_ids, acct, pick.get("owner", ""),
                           auto_tracking, pick.get("ping_env", ""))
    except Exception as e:  # Login/Incapsula/Netzwerk — nie den ganzen Bot reißen
        log.error("orders[%s]: fetch fehlgeschlagen: %s", pick["name"], e)
    # Immer speichern: last_check_at (in _check_one_account vor dem Login gesetzt)
    # muss persistiert werden — auch bei Fehler → Backoff statt Hämmern.
    orders.save_order_state(token, gist_id, st)
