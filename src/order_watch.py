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

from . import commands, hermes, notify, orders
from .config import parse_ids
from .embeds import (
    build_account_check_embed,
    build_account_idle_embed,
    build_order_status_embed,
    build_order_tracking_embed,
)

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
# So viele aufeinanderfolgende Abrufe muss „alles erledigt" gelten, bevor sich
# ein Konto selbst abschaltet.
#
# Einer reicht nicht, und das ist der ganze Grund für diese Zahl: Man schaltet
# das Konto ein, WEIL man gerade bestellt hat — im Shop steht die Bestellung
# dann aber oft noch gar nicht. Beim ersten Abruf sähe alles „erledigt" aus,
# das Konto schliefe sofort wieder ein und bekäme die eigene Bestellung nie zu
# sehen. Mit zwei Abrufen liegt zwischen Einschalten und Aufgeben mindestens
# ein voller Ruhe-Takt (24 h) — genug Zeit für jede Bestellung, aufzutauchen.
_SETTLED_RUNS_BEFORE_IDLE = 2


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("on", "an", "true", "yes", "y", "ja", "1")


def _account_settled(acct: dict, st: dict, name: str) -> bool:
    """Ist bei diesem Konto alles durch — Bestellungen fertig, nichts unterwegs?

    Dann gibt es aus dem Kundenkonto nichts mehr zu erfahren, und der Bot kann
    sich das Einloggen sparen, bis wieder bestellt wird. Drei Bedingungen, jede
    aus einem eigenen Grund:

    1. Es gab überhaupt schon Bestellungen. Ein Konto, das noch nie etwas
       gesehen hat, ist nicht „fertig" — es ist ungeprüft.
    2. Jede Bestellung steht auf einem Endzustand.
    3. Zu keiner ist noch ein Tracking-Link offen. „completed" heißt nicht, dass
       der Link schon eingesammelt wurde — und nach dem Abschalten käme niemand
       mehr an ihn heran.

    Die Sendung selbst hält das Konto NICHT wach: `hermes_watch` verfolgt sie
    ohne Login weiter. Trotzdem wird unten auf sie gewartet, damit „aus" auch
    das heißt, wonach es aussieht — nämlich dass die Sache erledigt ist.
    """
    bestellungen = list((acct.get("orders") or {}).values())
    if not bestellungen:
        return False
    for o in bestellungen:
        if o.get("status") not in _TERMINAL_STATUS:
            return False
        if o.get("status") in _TRACKABLE_STATUS and not o.get("tracking_registered"):
            return False

    # Sendungen dieses Kontos, die noch laufen. Einträge ohne `account` stammen
    # aus der Zeit vor diesem Feld — die halten nichts auf, sonst bliebe ein
    # Konto wegen einer längst zugestellten Altsendung für immer an.
    zustaende = st.get("manual_tracking_state") or {}
    for label, eintrag in (st.get("auto_tracking") or {}).items():
        if (eintrag or {}).get("account") != name:
            continue
        if not hermes.is_terminal((zustaende.get(label) or {}).get("status", "")):
            return False
    return True


def _order_enabled(st: dict, name: str, cmds: dict | None = None) -> bool:
    """An/Aus-Schalter aus dem Gist-Feld `enabled` — pro Konto:

    - Dict (empfohlen): `{"a": "on", "b": "off"}`  → einfach on/off pro Konto
    - `true`                                        → alle Konten an
    - Liste `["a"]`                                 → nur diese Konten an
    - fehlt / `false`                               → aus (kein Login)

    So aktivierst du Tracking nur wenn du wirklich bestellt hast (Gist editieren,
    kein Code-Commit) — in Bestellpausen also NULL authentifizierte Logins.

    `/account enable|disable` in Discord schreibt denselben Schalter nach
    `commands.json`; der hat Vorrang, weil er das Neuere ist. So kann man das
    Konto vom Handy aus scharf schalten, ohne JSON zu tippen.

    ACHTUNG: Das hier ist der WUNSCH, nicht der wirksame Zustand. Ist alles
    zugestellt, legt der Bot `_auto_off` in seinen eigenen Stand und ruht,
    obwohl hier noch „on" steht — er kann `commands.json` ja nicht
    zurückschreiben, die gehört dem Worker. Aufgehoben wird das erst, wenn der
    Wunsch von aus auf an wechselt, also beim nächsten `/account enable`.
    """
    per_command = commands.enabled_override(cmds or {}, name)
    if per_command is not None:
        return per_command

    en = st.get("enabled", False)
    if isinstance(en, dict):
        return _truthy(en.get(name, False))
    if en is True:
        return True
    if isinstance(en, list):
        return name in en
    return False


def _verify_pending(cmds: dict, name: str, acct: dict) -> bool:
    """Wartet dieses Konto noch auf seine erste Login-Prüfung?

    `verify` setzt der Worker beim Anlegen in `commands.json` — und löscht es
    nie wieder, denn diese Datei gehört ihm allein und er erfährt nichts vom
    Ausgang. Beendet wird die Prüfung stattdessen hier: Sobald
    `login_checked_at` im Stand des Bots steht, greift sie nicht mehr. So
    braucht es keine Quittung über die Dateigrenze hinweg.
    """
    if not name.startswith("s"):
        return False   # fest in config.yml verdrahtet — nichts zu prüfen
    slot = name[1:]
    eintrag = (cmds.get("accounts") or {}).get(slot) or {}
    return bool(eintrag.get("verify")) and not acct.get("login_checked_at")


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


def _remember_enabled(accounts_state: dict, cfg_accounts: list,
                      enabled_names: list) -> tuple[list[str], bool]:
    """An/Aus-Zustand jedes Kontos merken.

    Gibt (frisch eingeschaltete Konten, hat sich der Merker geändert) zurück.
    Das zweite Stück entscheidet, ob der Stand gespeichert werden muss.

    Der Merker wird für ALLE Konten gepflegt, auch die ausgeschalteten — sonst
    bliebe nach dem Ausschalten `True` stehen und das nächste Einschalten sähe
    aus wie „war doch schon an".

    Bewusst `is False` statt `not was_on`: Beim allerersten Lauf nach dieser
    Änderung fehlt der Merker überall. Ohne die Unterscheidung würde jedes
    bereits eingeschaltete Konto als „gerade eingeschaltet" gelten und einen
    überflüssigen Login auslösen. So wird beim ersten Lauf nur aufgeschrieben.
    """
    switched_on, changed = [], False
    for a in cfg_accounts:
        name = a["name"]
        acct = accounts_state.setdefault(name, {})
        now_on = name in enabled_names
        was_on = acct.get("_was_enabled")
        if now_on and was_on is False:
            switched_on.append(name)
        if was_on is not now_on:
            changed = True
        acct["_was_enabled"] = now_on
    return switched_on, changed


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
                       owner: str, ping_env: str, account: str = "") -> None:
    """Gefundene Tracking-Links in den Gist-Block `auto_tracking` schreiben.

    Damit übernimmt `hermes_watch` die Sendung ab dem nächsten Lauf von selbst:
    Verlauf scrapen, jedes neue Ereignis posten, bei Zustellung aufhören. Vorher
    endete die Kette bei der "Tracking ist da"-Karte — verfolgt wurden nur die
    von Hand ins Gist getippten Sendungen (`manual_tracking`).

    Label wie auf der Karte ("pray #10042"), damit Bestell- und Sendungskarten
    optisch zusammengehören. Discord-IDs landen NICHT im Gist — gespeichert wird
    nur der Name des Secrets, aufgelöst wird beim Posten.
    """
    base = f"{owner} #{order_id}" if owner else f"#{order_id}"
    for i, link in enumerate(links):
        label = base if i == 0 else f"{base} ({i + 1})"
        entry = {"url": link, "order_id": order_id}
        if account:
            # Der KONTOSCHLÜSSEL, nicht der Anzeigename: `_account_settled`
            # fragt damit, ob von diesem Konto noch etwas unterwegs ist.
            # Anzeigenamen sind frei wählbar und taugen nicht als Schlüssel.
            entry["account"] = account
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
            # Baseline: die Historie wird still übernommen, OFFENE Bestellungen
            # dagegen gemeldet — für deren Karte brauchen wir die Artikel.
            return o["status"] not in _TERMINAL_STATUS
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
        # Erstkontakt mit einem Konto. Die Bestellhistorie bleibt still — sonst
        # knallt bei jedem neu hinterlegten Konto die komplette Vergangenheit in
        # den Channel. Was noch LÄUFT wird dagegen gemeldet: Genau dafür trägt
        # man das Konto ja gerade ein, und es sind nie viele.
        #
        # `stale` prüft gegen _TERMINAL_STATUS statt nur gegen "completed" —
        # sonst bekäme eine stornierte Altbestellung später noch eine
        # Tracking-Karte hinterhergeworfen.
        offen = 0
        for o in olist:
            oid, slug = o["order_id"], o["status"]
            stale = slug in _TERMINAL_STATUS
            entry = {"status": slug, "tracking_posted": stale, "tracking_registered": stale}
            detail = orders.parse_order_detail(details[oid]) if oid in details else None
            items = (detail or {}).get("items") or None
            if items:
                entry["items"] = items
            order_map[oid] = entry
            if not stale:
                notify.send_order_update(
                    webhook,
                    build_order_status_embed(o, fresh=True, items=items, owner=owner),
                    ping_ids, role_ids,
                )
                offen += 1
        acct["_initialized"] = True
        log.info("orders[%s]: Erstkontakt — %d Bestellung(en) übernommen, %d offene gemeldet",
                 name, len(olist), offen)
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
                _register_tracking(auto_tracking, oid, detail["tracking"], owner, ping_env, name)
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

    # Per `/account add` selbst hinterlegte Konten kommen aus `commands.json`
    # dazu. Sie sehen aus wie Config-Einträge, verweisen aber auf die
    # vorverdrahteten Secret-Plätze aus main.yml. Fehlen deren Secrets (Platz
    # frei), fällt das Konto weiter unten bei der Secret-Prüfung ohnehin raus.
    cmds = commands.load_commands(token, gist_id)
    cfg_accounts = list(cfg_accounts) + commands.slot_accounts(cmds)

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
    #
    # Zwei Ebenen, und die Reihenfolge ist wichtig: ERST der Wunsch (Gist +
    # Discord), DANN das selbsttätige Abschalten. Die Unterscheidung braucht es,
    # weil `_remember_enabled` den Wechsel aus→an erkennen muss — und den sähe
    # es nie, wenn `_auto_off` schon vorher alles auf „aus" drückte. Das Konto
    # ließe sich dann nie wieder einschalten.
    wunsch_names = [a["name"] for a in cfg_accounts if _order_enabled(st, a["name"], cmds)]

    # Gerade eingeschaltet? Dann sofort prüfen statt bis zu 24 h Ruhe-Takt
    # abzuwarten — man legt den Schalter genau dann um, wenn man bestellt hat.
    # `last_check_at` leeren genügt: `_orders_due` wertet "noch nie geprüft"
    # bereits als fällig, und beim Sortieren rutscht das Konto nach vorn.
    #
    # Erkannt wird über den gemerkten Zustand, nicht über den Command — so
    # greift es auch, wenn der Schalter von Hand im Gist umgelegt wurde. Der
    # Merker wird SOFORT gespeichert: Weiter unten gibt es mehrere Abbruchwege,
    # auf denen er sonst verlorenginge und das Einschalten nie erkannt würde.
    switched_on, flags_changed = _remember_enabled(accounts_state, cfg_accounts, wunsch_names)
    for name in switched_on:
        accounts_state[name]["last_check_at"] = ""
        # Frisch scharf geschaltet → ein etwaiges Auto-Aus der letzten Bestellung
        # ist damit erledigt. Genau das macht `/account enable` zu einer Aussage
        # über EINE Bestellung statt zu einem Dauerzustand, den man vergisst.
        accounts_state[name].pop("_auto_off", None)
        # Auch den Zähler: Wer gerade einschaltet, hat gerade bestellt. Ein alter
        # Stand von „schon zweimal erledigt" würde sofort wieder abschalten.
        accounts_state[name].pop("_settled_runs", None)
    if switched_on:
        log.info("orders: gerade eingeschaltet, wird sofort geprüft: %s", ", ".join(switched_on))
    if flags_changed:
        orders.save_order_state(token, gist_id, st)

    # Der wirksame Zustand: gewünscht UND nicht selbsttätig abgeschaltet.
    enabled_names = [n for n in wunsch_names
                     if not accounts_state.get(n, {}).get("_auto_off")]

    # Frisch per `/account add` hinterlegte Konten müssen EINMAL geprüft werden,
    # auch wenn sie noch aus sind. Sie sind es nämlich immer: Neue Konten starten
    # ausgeschaltet, damit in Bestellpausen keine Logins passieren. Ohne diese
    # Ausnahme fiele das Konto hier raus, und wer gerade sein Passwort eingetippt
    # hat, erführe nie, ob es stimmt.
    #
    # Es bleibt bei genau EINEM Login: Danach steht `login_checked_at` im Stand,
    # `_verify_pending` greift nicht mehr, und das Konto ruht weiter bis zum
    # ersten `/account enable`.
    verify_names = [a["name"] for a in cfg_accounts
                    if _verify_pending(cmds, a["name"], accounts_state.get(a["name"], {}))]

    # Karteileichen wegräumen. `/account remove` löscht den Eintrag in
    # `commands.json`, kommt aber an diese Datei nicht heran — der Worker darf
    # sie bewusst nicht anfassen, sonst gingen Änderungen des Bots verloren.
    # Der Rest blieb deshalb für immer in `/account list` stehen, unter seinem
    # rohen Schlüssel (`s3`), ohne je wieder geprüft zu werden. Aufräumen kann
    # das nur, wem die Datei gehört: dieser Lauf hier.
    #
    # Die Bedingung `if cmds` ist die Absicherung, auf die es ankommt: Ist
    # `commands.json` gerade nicht lesbar, kommt {} zurück — dann sähen ALLE
    # selbst hinterlegten Konten wie Leichen aus, und ein Aussetzer beim Lesen
    # würde ihren ganzen Bestellverlauf löschen.
    if cmds:
        bekannt = {a["name"] for a in cfg_accounts}
        entfernt = [n for n in accounts_state if n not in bekannt]
        for name in entfernt:
            del accounts_state[name]
            (st.get("enabled") or {}).pop(name, None)
            log.info("orders: '%s' steht nirgends mehr — Reste entfernt", name)
        if entfernt:
            orders.save_order_state(token, gist_id, st)

    kandidaten = set(enabled_names) | set(verify_names)
    if not kandidaten:
        log.info("orders: alle Konten 'off' (Gist `enabled`) — übersprungen.")
        return

    # Nur diese Accounts, sofern Secrets (Login + Ziel-Webhook) vorhanden sind.
    resolved = []
    for a in cfg_accounts:
        if a["name"] not in kandidaten:
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

    # Frisch per `/account add` hinterlegte Konten warten auf ihre erste
    # Login-Prüfung — die geht IMMER vor. Ohne diesen Vorzug könnte sie hinter
    # dem Ruhe-Takt eines anderen Kontos bis zu 24 Stunden hängen, und derjenige,
    # der gerade sein Passwort eingetippt hat, wüsste solange nicht, ob es stimmt.
    wartend = [r for r in resolved
               if _verify_pending(cmds, r["name"], accounts_state.setdefault(r["name"], {}))]
    if not due and not wartend:
        log.info("orders: nichts fällig (%d Account(s))", len(resolved))
        return

    due.sort(key=lambda r: accounts_state[r["name"]].get("last_check_at", ""))  # "" zuerst, dann ältester
    pick = wartend[0] if wartend else due[0]

    acct = accounts_state[pick["name"]]
    # Gefundene Tracking-Links landen hier drin; `check_shipments` (läuft im
    # selben Run direkt danach) liest den Block und verfolgt die Sendung.
    auto_tracking = st.setdefault("auto_tracking", {})
    pruefung = _verify_pending(cmds, pick["name"], acct)
    fehler = ""
    try:
        _check_one_account(pick["name"], pick["webhook"], pick["user"], pick["pw"],
                           pick["ping"], role_ids, acct, pick.get("owner", ""),
                           auto_tracking, pick.get("ping_env", ""))
    except Exception as e:  # Login/Incapsula/Netzwerk — nie den ganzen Bot reißen
        fehler = str(e)
        log.error("orders[%s]: fetch fehlgeschlagen: %s", pick["name"], e)

    # Wie der Abruf ausging, bei JEDEM Lauf festhalten — nicht nur beim ersten.
    # `/account list` zeigte sonst „geprüft vor 5 min" und sah damit gleich aus,
    # egal ob der Login stand oder das Passwort seit Wochen falsch ist. Genau
    # das ist die Frage, die man an eine Kontoliste hat.
    acct["login_ok"] = not fehler

    # Ergebnis der Erstprüfung melden — einmalig, danach steht `login_checked_at`
    # im eigenen Stand des Bots und `_verify_pending` greift nicht mehr.
    # Bewusst hier und nicht im Worker: Erst dieser Lauf weiß, ob der Login geht.
    if pruefung:
        acct["login_checked_at"] = datetime.now(timezone.utc).isoformat()
        if pick["webhook"]:
            notify.send_order_update(
                pick["webhook"],
                build_account_check_embed(pick.get("owner") or pick["name"], not fehler, fehler),
                pick["ping"], role_ids,
            )
        log.info("orders[%s]: Erstprüfung %s", pick["name"], "ok" if not fehler else "fehlgeschlagen")

    # Alles zugestellt? Dann von selbst wieder ausschalten. Einschalten heißt
    # damit „ich habe bestellt", nicht „ab jetzt für immer" — und niemand muss
    # daran denken, es zurückzunehmen. Nur bei geglücktem Abruf: Nach einem
    # Fehlschlag ist der Stand von eben womöglich unvollständig, und ein Konto
    # wegen eines Netzfehlers stillzulegen wäre die falsche Schlussfolgerung.
    if not fehler and not acct.get("_auto_off"):
        if _account_settled(acct, st, pick["name"]):
            acct["_settled_runs"] = int(acct.get("_settled_runs") or 0) + 1
        else:
            # Wieder etwas los → der Zähler fängt von vorn an. Sonst könnte ein
            # halbes Jahr alter Ruhestand mit einem einzigen weiteren Abruf
            # zuschlagen, mitten in einer laufenden Bestellung.
            acct.pop("_settled_runs", None)
    if (not fehler and not acct.get("_auto_off")
            and int(acct.get("_settled_runs") or 0) >= _SETTLED_RUNS_BEFORE_IDLE):
        acct["_auto_off"] = True
        acct.pop("_settled_runs", None)
        log.info("orders[%s]: alles zugestellt — Konto ruht bis zum nächsten "
                 "/account enable", pick["name"])
        if pick["webhook"]:
            notify.send_order_update(
                pick["webhook"],
                build_account_idle_embed(pick.get("owner") or pick["name"]),
                [], [],   # keine Pings: Das ist eine Quittung, keine Neuigkeit
            )
    # Immer speichern: last_check_at (in _check_one_account vor dem Login gesetzt)
    # muss persistiert werden — auch bei Fehler → Backoff statt Hämmern.
    orders.save_order_state(token, gist_id, st)
