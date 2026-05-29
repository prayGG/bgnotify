"""Manueller Sicht-Test für die Bestell-Nachrichten.

Schickt jeden Order-Nachrichtentyp mit Beispieldaten in den Order-Channel, damit
man die Embeds ansehen und feintunen kann. KEIN BG-Login, KEIN Gist, KEIN echter
Bestell-Stand — nur die echten Embed-Builder aus `main.py` gegen den Webhook
`DISCORD_ORDER_WEBHOOK_URL`. Änderst du `build_order_*_embed`, zeigt der Test es
sofort. Wird über den `order-test`-Workflow (workflow_dispatch) ausgelöst.
"""
from __future__ import annotations

import logging
import os
import re
import sys

from . import notify
from .main import build_order_status_embed, build_order_tracking_embed

log = logging.getLogger(__name__)

# Beispiel-Bestellung (frei erfunden) — deckt den ganzen Lebenslauf + Sonderfälle ab.
_OID = "12345"
_ITEMS = ["GHK CU 100 mg × 2", "Roaccutane 20 mg × 3"]
SAMPLES: list[tuple[str, dict]] = [
    ("Neue Bestellung (pending)",
     build_order_status_embed({"order_id": _OID, "status": "pending", "status_text": "Pending payment"}, fresh=True, items=_ITEMS)),
    ("Status → Preparing",
     build_order_status_embed({"order_id": _OID, "status": "processing", "status_text": "Preparing"}, items=_ITEMS)),
    ("Status → On hold",
     build_order_status_embed({"order_id": _OID, "status": "on-hold", "status_text": "On hold"}, items=_ITEMS)),
    ("Status → Completed",
     build_order_status_embed({"order_id": _OID, "status": "completed", "status_text": "Completed"}, items=_ITEMS)),
    ("Status → Cancelled",
     build_order_status_embed({"order_id": _OID, "status": "cancelled", "status_text": "Cancelled"}, items=_ITEMS)),
    ("Tracking (mit Link)",
     build_order_tracking_embed(_OID, ["https://tracking.hermesworld.com/?TrackID=H1234567890BEISPIEL"], items=_ITEMS)),
]


def _post(webhook: str, embed: dict, ping_ids: list[str]) -> bool:
    """Beispiel-Embed posten — TEST-Username, pingt wie im Echtbetrieb."""
    prefix = " ".join(f"<@{i}>" for i in ping_ids)
    content = "🧪 Beispielnachricht (Test)" + (f"\n{prefix}" if prefix else "")
    payload = {
        "username": "bgnotify · orders · TEST",
        "content": content,
        "embeds": [embed],
        "allowed_mentions": {"users": [str(i) for i in ping_ids]} if ping_ids else {"parse": []},
    }
    return notify._request("POST", webhook, payload) is not None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    webhook = os.environ.get("DISCORD_ORDER_WEBHOOK_URL", "")
    if not webhook:
        log.error("DISCORD_ORDER_WEBHOOK_URL ist leer — nichts zu senden.")
        return 1
    ping_ids = [x for x in re.split(r"[,;\s]+", os.environ.get("DISCORDID", "")) if x]

    ok = True
    for label, embed in SAMPLES:
        sent = _post(webhook, embed, ping_ids)
        log.info("  %-26s %s", label, "ok" if sent else "FAIL")
        ok = ok and sent
    log.info("fertig — %d Beispielnachrichten %s%s", len(SAMPLES),
             "gesendet" if ok else "(mit Fehlern)", " (mit Ping)" if ping_ids else " (ohne Ping)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
