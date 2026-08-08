"""Prüft die Entscheidungen des Bots OHNE Netz, Browser oder Gist.

    python test_bot.py

Gegenstück zu `worker/test.mjs`, gleiche Machart: kein Framework, echte
Aufrufe, Attrappen nur an den Rändern (Gist-I/O, Login, Discord-Versand).

Geprüft wird vor allem, WELCHES Konto ein Lauf anfasst. Genau dort saß der
Fehler, der die Login-Prüfung nie feuern ließ: Frisch hinterlegte Konten sind
standardmäßig aus, und der Filter warf sie raus, bevor die Prüfung greifen
konnte. Ein Fehler, den man dem Code nicht ansieht — nur dem Ablauf.
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
logging.basicConfig(level=logging.CRITICAL)

from src import commands, notify, order_watch, orders  # noqa: E402

failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global failed
    print(f"{'  ok  ' if cond else ' FAIL '} {label}" + (f"  → {detail}" if detail else ""))
    if not cond:
        failed += 1


def section(t: str) -> None:
    print(f"\n── {t} ──")


class Lauf:
    """Ein Bot-Lauf mit ausgetauschten Rändern.

    `login_ok=False` lässt den Login werfen — genau wie ein falsches Passwort.
    """

    def __init__(self, *, cmds: dict, state: dict, login_ok: bool = True):
        self.cmds, self.state, self.login_ok = cmds, state, login_ok
        self.geprueft: list[str] = []      # welche Konten eingeloggt wurden
        self.karten: list[dict] = []       # was nach Discord ging
        self.quittungen: list[dict] = []   # Command-Antworten (eigener Channel, ohne Ping)
        self.gespeichert: dict = {}

    def __enter__(self):
        self._orig = (
            commands.load_commands, orders.load_order_state,
            orders.save_order_state, orders.fetch, notify.send_order_update,
            notify.send_command_result,
        )
        commands.load_commands = lambda *a, **k: self.cmds
        orders.load_order_state = lambda *a, **k: self.state
        orders.save_order_state = lambda t, g, s: self.gespeichert.update(s) or True
        orders.fetch = self._fetch
        notify.send_order_update = lambda hook, embed, *a, **k: self.karten.append(embed)
        # Getrennt gesammelt: Eine Command-Antwort DARF nicht über den
        # Meldungs-Versand laufen, sonst pingt sie und landet im falschen Channel.
        notify.send_command_result = lambda hook, embed: self.quittungen.append(
            {"hook": hook, "embed": embed})
        return self

    def __exit__(self, *exc):
        (commands.load_commands, orders.load_order_state, orders.save_order_state,
         orders.fetch, notify.send_order_update, notify.send_command_result) = self._orig
        return False

    def _fetch(self, user, pw, want_detail=None, cookies=None):
        self.geprueft.append(user)
        if not self.login_ok:
            raise RuntimeError("Login nicht erfolgreich")
        return [], {}, []

    def run(self, cfg):
        order_watch.check_orders(cfg, "https://discord/hook", ["1"], [])
        return self


CFG = {"orders": {"check_interval_minutes": 240, "idle_interval_minutes": 1440, "accounts": [
    {"name": "a", "label": "haupt", "username_env": "BG_USERNAME", "password_env": "BG_PASSWORD"},
]}}

os.environ.update({
    "GIST_TOKEN": "t", "GIST_ID": "g",
    "BG_USERNAME": "haupt@x.de", "BG_PASSWORD": "pw",
    "BG_USERNAME_3": "neu@x.de", "BG_PASSWORD_3": "pw3", "DISCORDID_3": "42",
})

# --------------------------------------------------------------------------
section("Welches Konto fasst ein Lauf an?")
# --------------------------------------------------------------------------
with Lauf(cmds={"enabled": {"a": "off"}}, state={"enabled": {}, "accounts": {}}) as l:
    l.run(CFG)
    check("alles aus → gar kein Login", l.geprueft == [], str(l.geprueft))

with Lauf(cmds={"enabled": {"a": "on"}}, state={"enabled": {}, "accounts": {}}) as l:
    l.run(CFG)
    check("eingeschaltet → wird geprüft", l.geprueft == ["haupt@x.de"], str(l.geprueft))

# Der eigentliche Fall: frisch hinterlegtes Konto, noch AUS, wartet auf Prüfung.
neu = {"accounts": {"3": {"label": "kollege", "verify": True}}, "enabled": {"a": "off"}}
with Lauf(cmds=neu, state={"enabled": {}, "accounts": {}}) as l:
    l.run(CFG)
    check("frisch hinterlegt und AUS → wird trotzdem einmal geprüft",
          l.geprueft == ["neu@x.de"], str(l.geprueft))
    check("… und meldet Erfolg", any("erfolgreich" in (k.get("description") or "") for k in l.karten),
          str([k.get("title") for k in l.karten]))

with Lauf(cmds=neu, state={"enabled": {}, "accounts": {}}, login_ok=False) as l:
    l.run(CFG)
    check("falsches Passwort → meldet Fehlschlag",
          any("fehlgeschlagen" in (k.get("description") or "") for k in l.karten),
          str([k.get("title") for k in l.karten]))
    check("… mit Grund", any("Login nicht erfolgreich" in (k.get("description") or "") for k in l.karten))

# Einmal geprüft = fertig. Sonst loggt sich ein ausgeschaltetes Konto ewig ein.
schon = {"accounts": {"s3": {"login_checked_at": "2026-08-07T00:00:00+00:00",
                             "login_ok": True, "last_check_at": "2026-08-07T00:00:00+00:00"}},
         "enabled": {}}
with Lauf(cmds=neu, state=schon) as l:
    l.run(CFG)
    check("schon geprüft und aus → kein Login mehr", l.geprueft == [], str(l.geprueft))
    check("… und keine zweite Karte", l.karten == [])

# --------------------------------------------------------------------------
section("Wunschzustand aus Discord")
# --------------------------------------------------------------------------
with Lauf(cmds={"enabled": {"a": "off"}}, state={"enabled": {"a": "on"}, "accounts": {}}) as l:
    l.run(CFG)
    check("/account disable schlägt den Gist-Stand", l.geprueft == [], str(l.geprueft))

with Lauf(cmds={"enabled": {"a": "on"}}, state={"enabled": {"a": "off"}, "accounts": {}}) as l:
    l.run(CFG)
    check("/account enable ebenso", l.geprueft == ["haupt@x.de"], str(l.geprueft))

# --------------------------------------------------------------------------
section("Slot-Konten");
# --------------------------------------------------------------------------
sa = commands.slot_accounts({"accounts": {"3": {"label": "kollege"}, "nope": {}}})
check("Platz wird zu einem Konto-Eintrag", [a["name"] for a in sa] == ["s3"], str([a["name"] for a in sa]))
check("verweist auf die Secret-Namen", sa[0]["username_env"] == "BG_USERNAME_3" and sa[0]["ping_env"] == "DISCORDID_3")
check("unbrauchbarer Schlüssel fliegt raus", all(a["name"] != "snope" for a in sa))

# --------------------------------------------------------------------------
section("Produkte aus Discord")
# --------------------------------------------------------------------------
from src import bgpharma, product_watch  # noqa: E402

PROD = "https://bgpharmadrugs.to/product/peptides/"

cfg_mit_produkt = {"products": [{"name": "Fest", "url": PROD, "watch_variants": ["X"]}]}
zusammen = product_watch.merge_products(cfg_mit_produkt, {})
check("ohne Gist bleibt es bei config.yml", len(zusammen) == 1)

zusammen = product_watch.merge_products(
    {"products": []}, {"products": {"k": {"url": PROD, "name": "BPC157 10mg", "variants": ["BPC157 10mg"]}}})
check("Command-Produkt kommt dazu", len(zusammen) == 1 and zusammen[0]["name"] == "BPC157 10mg", str(zusammen))
check("Wortlaut der Variante bleibt roh", zusammen[0]["watch_variants"] == ["BPC157 10mg"])

# Gleiche URL in beiden: die von Hand gepflegte Config gewinnt, sonst
# ueberschriebe ein Command die Aliase und Kommentare aus der YAML.
zusammen = product_watch.merge_products(
    cfg_mit_produkt, {"products": {"k": {"url": PROD, "name": "Doppelt"}}})
check("dieselbe URL → config.yml gewinnt", len(zusammen) == 1 and zusammen[0]["name"] == "Fest", str(zusammen))


class ScanLauf(Lauf):
    """Wie Lauf, aber mit ausgetauschtem Seiten-Leser."""

    def __init__(self, *, cmds, state, daten=None, kaputt=False):
        super().__init__(cmds=cmds, state=state)
        self.daten, self.kaputt, self.gelesen = daten, kaputt, []

    def __enter__(self):
        super().__enter__()
        self._lv = bgpharma.list_variants
        bgpharma.list_variants = self._list
        return self

    def __exit__(self, *exc):
        bgpharma.list_variants = self._lv
        return super().__exit__(*exc)

    def _list(self, url, session=None):
        self.gelesen.append(url)
        if self.kaputt:
            raise RuntimeError("HTTP 404")
        return dict(self.daten)

    def scans(self):
        product_watch.run_scans({}, BOT_HOOK)
        return self


BOT_HOOK = "https://discord/bot-channel"
auftrag = {"scans": {PROD: {"requested_at": "x"}}}
gefunden = {"title": "Peptides and HGH", "simple": False, "variants": ["BPC157 10mg"]}

with ScanLauf(cmds=auftrag, state={}, daten=gefunden) as l:
    l.scans()
    check("offener Auftrag → Seite wird gelesen", l.gelesen == [PROD], str(l.gelesen))
    check("Ergebnis landet im Stand des Bots", PROD in l.gespeichert.get("product_scans", {}))
    check("Varianten gemerkt", l.gespeichert["product_scans"][PROD]["variants"] == ["BPC157 10mg"])
    check("meldet den Fund",
          any("Varianten gefunden" in (q["embed"].get("description") or "") for q in l.quittungen))
    # Eine Command-Antwort ist eine Quittung, keine Meldung: eigener Channel,
    # kein Ping. Ginge sie über send_order_update, stünde sie im Bestell-Channel
    # und würde alle anpingen, die dort auf Restocks warten.
    check("… im Bot-Channel", [q["hook"] for q in l.quittungen] == [BOT_HOOK],
          str([q["hook"] for q in l.quittungen]))
    check("… und nicht über den Meldungs-Versand (der pingt)", l.karten == [],
          str([k.get("title") for k in l.karten]))

# Schon eingelesen → nicht nochmal. Sonst laege bei jedem Lauf dieselbe Seite an.
fertig = {"product_scans": {PROD: {"title": "x", "simple": True, "variants": []}}}
with ScanLauf(cmds=auftrag, state=fertig, daten=gefunden) as l:
    l.scans()
    check("schon eingelesen → kein zweiter Abruf", l.gelesen == [], str(l.gelesen))
    check("… und keine zweite Karte", l.karten == [])

with ScanLauf(cmds=auftrag, state={}, kaputt=True) as l:
    l.scans()
    check("Seite kaputt → Fehler wird festgehalten",
          l.gespeichert["product_scans"][PROD].get("error", "").startswith("HTTP 404"))
    check("… und gemeldet statt verschluckt",
          any("nicht lesbar" in (q["embed"].get("title") or "") for q in l.quittungen),
          str([q["embed"].get("title") for q in l.quittungen]))

print(f"\n{failed} FEHLER" if failed else "\nALLES GRÜN")
sys.exit(1 if failed else 0)
