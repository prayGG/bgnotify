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
import sys

from . import notify
from .main import build_order_status_embed, build_order_tracking_embed

log = logging.getLogger(__name__)

# Beispiel-Bestellung (frei erfunden) — deckt den ganzen Lebenslauf + Sonderfälle ab.
_OID = "12345"
SAMPLES: list[tuple[str, dict]] = [
    ("Neue Bestellung (pending)",
     build_order_status_embed({"order_id": _OID, "status": "pending", "status_text": "Pending payment"}, fresh=True)),
    ("Status → Preparing",
     build_order_status_embed({"order_id": _OID, "status": "processing", "status_text": "Preparing"})),
    ("Status → On hold",
     build_order_status_embed({"order_id": _OID, "status": "on-hold", "status_text": "On hold"})),
    ("Status → Completed",
     build_order_status_embed({"order_id": _OID, "status": "completed", "status_text": "Completed"})),
    ("Status → Cancelled",
     build_order_status_embed({"order_id": _OID, "status": "cancelled", "status_text": "Cancelled"})),
    ("Tracking (mit Link)",
     build_order_tracking_embed(_OID, ["https://tracking.hermesworld.com/?TrackID=H1234567890BEISPIEL"])),
]


def _post(webhook: str, embed: dict) -> bool:
    """Beispiel-Embed posten — eigener TEST-Username, kein Ping (kein Spam)."""
    payload = {
        "username": "bgnotify · orders · TEST",
        "content": "🧪 Beispielnachricht (Test)",
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }
    return notify._request("POST", webhook, payload) is not None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    webhook = os.environ.get("DISCORD_ORDER_WEBHOOK_URL", "")
    if not webhook:
        log.error("DISCORD_ORDER_WEBHOOK_URL ist leer — nichts zu senden.")
        return 1

    ok = True
    for label, embed in SAMPLES:
        sent = _post(webhook, embed)
        log.info("  %-26s %s", label, "ok" if sent else "FAIL")
        ok = ok and sent
    log.info("fertig — %d Beispielnachrichten %s", len(SAMPLES), "gesendet" if ok else "(mit Fehlern)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
