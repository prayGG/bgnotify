"""Hermes-Sendungsverfolgung: kompletten Sendungsverlauf zu einer Tracking-URL holen.

Ein offizielles API gibt es nicht — Hermes' Track&Trace-Schnittstelle setzt einen
Vertrag voraus. Deshalb wird die öffentliche Sendungsseite mit Playwright geladen
(sie rendert den Verlauf per JS nach) und ausgewertet.

Seitenaufbau (Stand 2026-06):

    Deine Sendungsdetails
    Deine Sendung
    H1000000000000000001                     <- Sendungsnummer
    Sendung wurde an der Empfangsadresse zugestellt.   <- Kurzstatus
    Zugestellt am:Freitag, 12.06.2026        <- Detailfelder "Label:Wert"
    Uhrzeit:14:28 Uhr
    Zustellort:Empfangsadresse
    Sendungsverlauf                          <- ab hier die Ereignisse
    Sonntag, 07.06.2026
    22:02
    Die Sendung wurde Hermes elektronisch angekündigt. ...
    ...                                      <- ÄLTESTE zuerst, neueste zuletzt

Debug:
    python -m src.hermes "https://tracking.hermesworld.com/?TrackID=..."
"""
from __future__ import annotations

import hashlib
import logging
import re
import sys
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_WEEKDAYS = "Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag"
_DATE_RE = re.compile(rf"^(?:{_WEEKDAYS}),?\s*(\d{{1,2}}\.\d{{1,2}}\.\d{{2,4}})\s*$", re.I)
_TIME_RE = re.compile(r"^(\d{1,2}:\d{2})(\s*Uhr)?\s*$", re.I)
_FIELD_RE = re.compile(r"^([A-Za-zÄÖÜäöü][A-Za-zÄÖÜäöü .\-]{2,30}?):\s*(.+)$")
_NUMBER_RE = re.compile(r"^[A-Z]?\d{12,25}$")

_HISTORY_MARKER = "sendungsverlauf"

# Ab hier beginnt der Seitenfuß (Navigation, Rechtliches). Ohne diese Grenze
# hängt sich der ganze Footer an das letzte Ereignis, weil danach keine
# Datumszeile mehr folgt, an der das Sammeln enden würde.
_FOOTER_MARKERS = (
    # Seitenfuß
    "schnelleinstieg", "kundenservice", "globale hermes links",
    "impressum", "datenschutz", "deine vorteile", "hermes germany gmbh",
    # Formularblock unter dem Verlauf (Benachrichtigungen / PLZ-Abfrage) — taucht
    # bei noch nicht zugestellten Sendungen auf.
    "pflichtfeld", "benachrichtigungen aktivieren", "empfänger-plz",
    "zu deiner sicherheit", "e-mail benachrichtigung",
)
_MAX_EVENT_CHARS = 400
_TERMINAL = ("zugestellt", "zustellung erfolgt", "ausgeliefert", "empfangen",
             "zurückgesendet", "retoure", "abgeholt")


def is_terminal(status: str) -> bool:
    """Sendung abgeschlossen? Dann muss nicht weiter gepollt werden."""
    s = (status or "").lower()
    return any(t in s for t in _TERMINAL)


def event_key(e: dict) -> str:
    """Stabiler Fingerabdruck eines Ereignisses.

    Damit erkennen wir Neues unabhängig davon, in welcher Reihenfolge Hermes
    den Verlauf ausliefert (die Seite zeigt neueste zuerst, der Textabzug
    älteste zuerst) und ohne den Wortlaut kennen zu müssen.
    """
    raw = f"{e.get('date','')}|{e.get('time','')}|{e.get('text','')}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:12]


def event_sort_key(e: dict) -> tuple:
    """Chronologisch sortierbar. Unparsbares wandert ans Ende (statt zu knallen)."""
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%y %H:%M", "%d.%m.%Y", "%d.%m.%y"):
        stamp = f"{e.get('date','')} {e.get('time','')}".strip()
        try:
            return (0, datetime.strptime(stamp, fmt))
        except ValueError:
            continue
    return (1, datetime.max)


def _fetch_text(url: str, timeout_ms: int = 45000) -> str:
    """Sendungsseite rendern und den sichtbaren Text zurückgeben."""
    from playwright.sync_api import sync_playwright  # noqa: WPS433

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            ctx = browser.new_context(
                user_agent=UA, locale="de-DE",
                # Ohne timezone_id nimmt Playwright die Systemzeitzone — auf dem
                # GitHub-Runner ist das UTC. Hermes formatiert seine Zeitstempel
                # im Browser, dadurch stand 02:48 als 00:48 auf der Karte. Die
                # Seite muss so gelesen werden, wie sie ein Empfänger hier sieht.
                timezone_id="Europe/Berlin",
                viewport={"width": 1280, "height": 900},
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = ctx.new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            try:
                page.wait_for_timeout(1500)   # letzte Nachzügler
            except Exception:
                pass
            return page.inner_text("body")
        finally:
            browser.close()


def _lines(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", l).strip() for l in (text or "").splitlines() if l.strip()]


def parse_shipment(text: str) -> dict:
    """Seitentext → {number, summary, details, events}.

    `events` ist chronologisch (älteste zuerst, wie auf der Seite) mit je
    `date`, `time` und `text`. Fehlt etwas, bleibt das Feld leer — nie raten.
    """
    lines = _lines(text)
    out: dict = {"number": "", "summary": "", "details": {}, "events": []}

    # Verlauf vom Kopfbereich trennen.
    split = next((i for i, l in enumerate(lines) if l.lower() == _HISTORY_MARKER), None)
    head = lines[:split] if split is not None else lines
    body = lines[split + 1:] if split is not None else []

    # Seitenfuß abschneiden — sonst landet die Navigation im letzten Ereignis.
    cut = next((i for i, l in enumerate(body)
                if any(m in l.lower() for m in _FOOTER_MARKERS)), None)
    if cut is not None:
        body = body[:cut]

    # --- Kopf: Sendungsnummer, Kurzstatus, Detailfelder ---
    for line in head:
        if not out["number"] and _NUMBER_RE.match(line.replace(" ", "")):
            out["number"] = line.replace(" ", "")
            continue
        m = _FIELD_RE.match(line)
        if m:
            out["details"][m.group(1).strip()] = m.group(2).strip()
            continue
        # Erster längerer Satz nach der Nummer = Kurzstatus
        if out["number"] and not out["summary"] and len(line) > 15 and ":" not in line:
            out["summary"] = line

    # --- Verlauf: Datum / Uhrzeit / Text ---
    cur_date = ""
    cur_time = ""
    buf: list[str] = []

    def flush() -> None:
        if cur_date and buf:
            # Deckel als zweites Netz, falls ein unbekannter Footer durchrutscht.
            text = " ".join(buf).strip()
            if len(text) > _MAX_EVENT_CHARS:
                text = text[:_MAX_EVENT_CHARS].rsplit(" ", 1)[0] + " …"
            out["events"].append({"date": cur_date, "time": cur_time, "text": text})

    for line in body:
        md = _DATE_RE.match(line)
        if md:
            flush()
            buf = []
            cur_date, cur_time = md.group(1), ""
            continue
        mt = _TIME_RE.match(line)
        if mt and cur_date and not buf:
            cur_time = mt.group(1)
            continue
        if cur_date:
            buf.append(line)
    flush()

    # Immer chronologisch (älteste zuerst) — die Seite liefert je nach Ansicht
    # mal so, mal so. Danach kann sich der Rest des Codes darauf verlassen.
    out["events"].sort(key=event_sort_key)

    # Kurzstatus notfalls aus dem jüngsten Ereignis ableiten.
    if not out["summary"] and out["events"]:
        out["summary"] = out["events"][-1]["text"]

    return out


def fetch_shipment(url: str) -> Optional[dict]:
    """Sendungsdaten holen; None wenn die Seite nicht lesbar/auswertbar ist."""
    try:
        text = _fetch_text(url)
    except Exception as e:
        log.warning("hermes: Seite nicht ladbar (%s): %s", url[:60], e)
        return None
    data = parse_shipment(text)
    if not data["events"] and not data["summary"]:
        log.warning("hermes: nichts erkennbar — Layout geändert? (%s)", url[:60])
        return None
    return data


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: python -m src.hermes <tracking-url>")
        raise SystemExit(2)
    raw = _fetch_text(sys.argv[1])
    data = parse_shipment(raw)
    print(f"Nummer : {data['number']}")
    print(f"Status : {data['summary']}")
    for k, v in data["details"].items():
        print(f"  {k}: {v}")
    print(f"Ereignisse ({len(data['events'])}):")
    for e in data["events"]:
        print(f"  {e['date']} {e['time']}  {e['text']}")
