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


def _orders_due(acct: dict, interval_minutes: int, idle_interval_minutes: int) -> bool:
    """Ist dieser Account fällig? Zwei-Gang-Takt (offen→schnell, sonst Ruhe) mit
    ±10% Jitter. Kein last_check_at = noch nie geprüft = fällig."""
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


def _check_one_account(name: str, webhook: str, user: str, pw: str,
                       ping_ids: list[str], role_ids: list[str], acct: dict,
                       owner: str = "") -> None:
    """Login + Diff + Post für GENAU einen Account; mutiert `acct` in place.

    last_check_at wird VOR dem Login gesetzt → Fehler-Drossel (ein gescheiterter
    Login löst trotzdem den Backoff aus, kein Hämmern). fetch() darf werfen —
    der Aufrufer fängt das und speichert den Stand (mit gesetztem last_check_at).
    """
    order_map = acct.setdefault("orders", {})
    initialized = acct.get("_initialized", False)
    acct["last_check_at"] = datetime.now(timezone.utc).isoformat()

    def want_detail(o: dict) -> bool:
        if not initialized:
            return False  # Baseline-Lauf: keine History nachladen
        prev = order_map.get(o["order_id"])
        if prev is None:
            return True  # neue Bestellung → Detailseite für die Artikel (+ ggf. Tracking)
        if o["status"] not in _TRACKABLE_STATUS:
            return False
        return not prev.get("tracking_posted")

    olist, details, cookies = orders.fetch(user, pw, want_detail, cookies=acct.get("cookies"))
    acct["cookies"] = cookies  # Session für nächsten Lauf merken (privates Gist)

    if not initialized:
        for o in olist:
            order_map[o["order_id"]] = {"status": o["status"], "tracking_posted": o["status"] == "completed"}
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
            order_map[oid] = {"status": slug, "tracking_posted": stale}
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

        if not prev.get("tracking_posted") and detail and detail.get("tracking"):
            notify.send_order_update(webhook, build_order_tracking_embed(oid, detail["tracking"], items=prev.get("items"), url=o.get("url"), owner=owner), ping_ids, role_ids)
            prev["tracking_posted"] = True


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
                         "ping": ping, "owner": a.get("label") or ""})

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
    try:
        _check_one_account(pick["name"], pick["webhook"], pick["user"], pick["pw"],
                           pick["ping"], role_ids, acct, pick.get("owner", ""))
    except Exception as e:  # Login/Incapsula/Netzwerk — nie den ganzen Bot reißen
        log.error("orders[%s]: fetch fehlgeschlagen: %s", pick["name"], e)
    # Immer speichern: last_check_at (in _check_one_account vor dem Login gesetzt)
    # muss persistiert werden — auch bei Fehler → Backoff statt Hämmern.
    orders.save_order_state(token, gist_id, st)
