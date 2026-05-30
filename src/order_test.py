"""Order-Test mit Live-Diagnose.

Postet in den Order-Channel:
  1. Eine **Diagnose**-Nachricht: Secrets/Verdrahtung, Gist-Stand und einen
     ECHTEN Login + Parse pro Konto (zeigt deine echten Bestellungen, Status,
     Artikel, Tracking — beweist die ganze Kette live).
  2. Die 6 Beispiel-Nachrichten (jeder Embed-Typ) zum Ansehen/Feintunen.

KEIN Schreiben ins Gist (read-only Test). Macht echte Logins → manuell auslösen
(Actions → order-test). Nutzt die echten Builder/Parser aus main.py/orders.py.
"""
from __future__ import annotations

import logging
import os
import re
import sys

import yaml

from . import notify, orders
from .main import CONFIG_PATH, build_order_status_embed, build_order_tracking_embed

log = logging.getLogger(__name__)

# --- Beispiel-Bestellung für die Render-Vorschau ---------------------------
_OID = "12345"
_URL = f"https://bgpharmadrugs.to/my-account/view-order/{_OID}/"
_ITEMS = ["GHK CU 100 mg × 2", "Roaccutane 20 mg × 3"]


def _o(status: str, text: str) -> dict:
    return {"order_id": _OID, "status": status, "status_text": text, "url": _URL}


SAMPLES: list[tuple[str, dict]] = [
    ("Neue Bestellung (pending)", build_order_status_embed(_o("pending", "Pending payment"), fresh=True, items=_ITEMS)),
    ("Status → Preparing", build_order_status_embed(_o("processing", "Preparing"), items=_ITEMS)),
    ("Status → On hold", build_order_status_embed(_o("on-hold", "On hold"), items=_ITEMS)),
    ("Status → Completed", build_order_status_embed(_o("completed", "Completed"), items=_ITEMS)),
    ("Status → Cancelled", build_order_status_embed(_o("cancelled", "Cancelled"), items=_ITEMS)),
    ("Tracking (mit Link)", build_order_tracking_embed(_OID, ["https://tracking.hermesworld.com/?TrackID=H1234567890BEISPIEL"], items=_ITEMS, url=_URL)),
]

_SECRET_KEYS = [
    "DISCORD_ORDER_WEBHOOK_URL", "GIST_TOKEN", "GIST_ID",
    "BG_USERNAME", "BG_PASSWORD", "DISCORDID",
    "BG_USERNAME_2", "BG_PASSWORD_2", "WEITERE_ID_HIER",
]


def _yesno(b: bool) -> str:
    return "✅" if b else "❌"


def _post_raw(webhook: str, embed: dict, ping_ids: list[str]) -> bool:
    prefix = " ".join(f"<@{i}>" for i in ping_ids)
    content = "🧪 Beispielnachricht (Test)" + (f"\n{prefix}" if prefix else "")
    payload = {
        "username": "bgnotify · orders · TEST",
        "content": content,
        "embeds": [embed],
        "allowed_mentions": {"users": [str(i) for i in ping_ids]} if ping_ids else {"parse": []},
    }
    return notify._request("POST", webhook, payload) is not None


def _diag_secrets() -> list[str]:
    out = ["**1) Secrets / Verdrahtung**"]
    for k in _SECRET_KEYS:
        out.append(f"{_yesno(bool(os.environ.get(k)))} `{k}`")
    return out


def _diag_gist(token: str, gist_id: str) -> tuple[list[str], dict]:
    out = ["", "**2) Gist / Stand**"]
    if not (token and gist_id):
        out.append("❌ GIST_TOKEN/GIST_ID fehlen")
        return out, {}
    st = orders.load_order_state(token, gist_id)
    out.append(f"{_yesno(isinstance(st, dict))} Gist gelesen")
    out.append(f"enabled: `{st.get('enabled')}`")
    for nm, a in (st.get("accounts") or {}).items():
        out.append(
            f"· Konto **{nm}**: {len(a.get('orders', {}))} Orders · "
            f"last_check `{(a.get('last_check_at') or '-')[:16]}` · "
            f"Cookies {_yesno(bool(a.get('cookies')))}"
        )
    return out, st


def _diag_live(cfg: dict, st: dict) -> list[str]:
    out = ["", "**3) Live-Login + Parsen** (echte Daten)"]
    accounts = (cfg.get("orders") or {}).get("accounts") or []
    state_accts = (st or {}).get("accounts") or {}
    any_tested = False
    for a in accounts:
        name = a["name"]
        user = os.environ.get(a.get("username_env", "BG_USERNAME"), "")
        pw = os.environ.get(a.get("password_env", "BG_PASSWORD"), "")
        if not (user and pw):
            out.append(f"· Konto **{name}**: ⏭️ keine Credentials")
            continue
        any_tested = True
        cookies = (state_accts.get(name) or {}).get("cookies")
        try:
            first: list[str] = []

            def _want(o: dict, _f=first) -> bool:
                if not _f:
                    _f.append(o["order_id"])
                    return True
                return False

            olist, details, _ck = orders.fetch(user, pw, _want, cookies=cookies)
            statuses = ", ".join(f"#{o['order_id']}={o['status']}" for o in olist[:6]) or "—"
            out.append(f"· Konto **{name}**: ✅ Login OK · {len(olist)} Orders: {statuses}")
            if olist and details:
                fid = olist[0]["order_id"]
                d = orders.parse_order_detail(details.get(fid, ""))
                items = ", ".join(d.get("items") or []) or "—"
                out.append(f"   ↳ #{fid}: Artikel: {items} · Tracking-Links: {len(d.get('tracking') or [])}")
        except Exception as e:  # Login/Incapsula/Parse — im Report zeigen, nicht crashen
            out.append(f"· Konto **{name}**: ❌ Fehler: {str(e)[:140]}")
    if not any_tested:
        out.append("(kein Konto mit Credentials — Login nicht getestet)")
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    webhook = os.environ.get("DISCORD_ORDER_WEBHOOK_URL", "")
    if not webhook:
        log.error("DISCORD_ORDER_WEBHOOK_URL ist leer — nichts zu senden.")
        return 1

    cfg = yaml.safe_load(open(CONFIG_PATH, encoding="utf-8")) or {}
    token, gist_id = os.environ.get("GIST_TOKEN", ""), os.environ.get("GIST_ID", "")

    # --- Diagnose zusammenbauen + posten ---
    lines = ["🧪 **Order-Test · Diagnose**", ""]
    lines += _diag_secrets()
    gist_lines, st = _diag_gist(token, gist_id)
    lines += gist_lines
    lines += _diag_live(cfg, st)
    diag_embed = {
        "author": {"name": "✦⠀⠀Order-Test · Diagnose⠀⠀✦"},
        "description": "\n".join(lines)[:4000],
        "color": 0x5865F2,
    }
    notify._request("POST", webhook, {
        "username": "bgnotify · orders · TEST",
        "content": "",
        "embeds": [diag_embed],
        "allowed_mentions": {"parse": []},
    })
    log.info("Diagnose gepostet")

    # --- Render-Vorschau aller Nachrichtentypen ---
    ping_ids = [x for x in re.split(r"[,;\s]+", os.environ.get("DISCORDID", "")) if x]
    ok = True
    for label, embed in SAMPLES:
        sent = _post_raw(webhook, embed, ping_ids)
        log.info("  %-26s %s", label, "ok" if sent else "FAIL")
        ok = ok and sent
    log.info("fertig — Diagnose + %d Beispielnachrichten%s", len(SAMPLES), " (mit Ping)" if ping_ids else "")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
