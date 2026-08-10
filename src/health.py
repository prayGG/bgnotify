"""Fehler-Report: ERROR-Logs des Runs → Embed in den Updates-Channel.

Ein Handler am Root-Logger sammelt alle ERROR-Records des Laufs, egal aus
welchem Modul. Am Run-Ende geht EIN Embed in den Updates-Channel — aber nur,
wenn sich das Fehlerbild geändert hat (Fingerprint in state.json), damit ein
dauerhaft kaputter Check nicht alle 10 Minuten denselben Alarm feuert.
Verschwinden die Fehler wieder, gibt es einmalig eine Entwarnung.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

from . import notify
from .embeds import build_error_embed, build_recovery_embed

log = logging.getLogger(__name__)

# Flappt das Fehlerbild (z. B. Timeout und Connection-Reset im Wechsel), wäre
# jeder Wechsel ein "neuer" Fingerprint und würde sofort wieder alarmieren —
# frühestens nach dieser Sperre geht der nächste Report raus.
_RESEND_COOLDOWN_SECONDS = 6 * 60 * 60

# requests-Exceptions enthalten die Request-URL — bei notify-Fehlern wäre das
# die geheime Webhook-URL. Vor dem Posten rausschneiden.
_WEBHOOK_URL = re.compile(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\S+")


class ErrorCollector(logging.Handler):
    """Sammelt alle ERROR-Records des Runs (dedupliziert, Reihenfolge bleibt)."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            origin = record.name.rsplit(".", 1)[-1]
            msg = _WEBHOOK_URL.sub("<webhook>", f"**{origin}**⠀{record.getMessage()}")
        except Exception:
            return
        if msg not in self.messages:
            self.messages.append(msg)


def install() -> ErrorCollector:
    """Collector an den Root-Logger hängen — vor den Watchern aufrufen."""
    collector = ErrorCollector()
    logging.getLogger().addHandler(collector)
    return collector


def _fingerprint(messages: list[str]) -> str:
    return hashlib.sha1("\n".join(sorted(messages)).encode()).hexdigest()


def report(state: dict, webhook: str, collector: ErrorCollector) -> None:
    """Fehlerbild des Runs melden bzw. Entwarnung geben.

    State-Felder (in state.json, nur Hash + Zeitstempel — keine Fehlertexte,
    das Repo ist öffentlich): `fingerprint`, `last_sent_at`, `active`.
    """
    report_state = state.setdefault("error_report", {})
    messages = collector.messages

    if not messages:
        if report_state.get("active"):
            if not webhook or notify.send_update_announcement(webhook, build_recovery_embed()):
                state["error_report"] = {"active": False}
        return

    fp = _fingerprint(messages)
    if fp == report_state.get("fingerprint"):
        return  # exakt dieses Fehlerbild wurde schon gemeldet — still bleiben
    if report_state.get("active") and report_state.get("last_sent_at"):
        try:
            sent_at = datetime.fromisoformat(report_state["last_sent_at"])
            if (datetime.now(timezone.utc) - sent_at).total_seconds() < _RESEND_COOLDOWN_SECONDS:
                return  # Fehlerbild flappt — Cooldown abwarten, alter Fingerprint bleibt
        except (ValueError, TypeError):
            pass
    if not webhook:
        log.warning("%d Fehler im Run, aber Updates-Webhook ist leer", len(messages))
        return
    if notify.send_update_announcement(webhook, build_error_embed(messages)):
        state["error_report"] = {
            "active": True,
            "fingerprint": fp,
            "last_sent_at": datetime.now(timezone.utc).isoformat(),
        }
