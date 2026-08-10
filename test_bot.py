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
from src.config import variant_labels  # noqa: E402

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
        notify.send_command_result = lambda app_id, token, embed, components=None: (
            self.quittungen.append({"app_id": app_id, "token": token, "embed": embed}) or True)
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

# /product rename — der gefaehrliche Teil ist, dass NUR die Anzeige wechselt.
# Benennt man den Match-String mit um, greift der Abgleich gegen das Dropdown
# der Seite ins Leere: Das Produkt stuende weiter im Dashboard und waere nie
# wieder auf Lager, ohne dass irgendetwas nach einem Fehler aussieht.
LANG = "Azelaic Acid 20% 30 gr cream"
umbenannt = commands.command_products(
    {"products": {"k": {"url": PROD, "name": LANG, "variants": [LANG], "label": "Azelaic 20% 30g"}}})[0]
check("umbenannt → kurze Anzeige", umbenannt["name"] == "Azelaic 20% 30g", umbenannt["name"])
check("… aber Match-String unveraendert", umbenannt["watch_variants"] == [LANG],
      str(umbenannt["watch_variants"]))
check("… als Alias wie in der config.yml",
      variant_labels({"products": [umbenannt]}) == {LANG: "Azelaic 20% 30g"})
ohne_label = commands.command_products(
    {"products": {"k": {"url": PROD, "name": LANG, "variants": [LANG]}}})[0]
check("ohne Umbenennung keine Aliase", "variant_labels" not in ohne_label)

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
        product_watch.run_scans({})
        return self


auftrag = {"scans": {PROD: {"requested_at": "x", "app_id": "app-1", "token": "tok-1"}}}
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
    # Der Interaction-Token aus dem Auftrag ist der ganze Punkt: Damit landet die
    # Antwort im Channel, in dem der Command getippt wurde, und nur dort.
    check("… als Antwort auf den auslösenden Command",
          [(q["app_id"], q["token"]) for q in l.quittungen] == [("app-1", "tok-1")],
          str([(q["app_id"], q["token"]) for q in l.quittungen]))
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

# --------------------------------------------------------------------------
section("Sortierung im Dashboard")
# --------------------------------------------------------------------------
from src.embeds import build_dashboard_embed, dashboard_group_names  # noqa: E402


def st(name, in_stock):
    return {"variant": name, "product_name": name, "in_stock": in_stock, "found": True,
            "price": "10", "product_url": "https://x"}


lage = [st("Zebra", True), st("Alpha", True), st("Beta", False)]
check("Überschriften wie angezeigt", dashboard_group_names(lage) == ["Zebra", "Alpha", "Beta"])

def reihenfolge(order):
    text = build_dashboard_embed(lage, order=order)["description"]
    return [n for n in ("Zebra", "Alpha", "Beta") if n in text] and sorted(
        ("Zebra", "Alpha", "Beta"), key=lambda n: text.index(n))

check("ohne Positionen: verfügbar oben, dann alphabetisch",
      reihenfolge({}) == ["Alpha", "Zebra", "Beta"], str(reihenfolge({})))
check("Position hebt Zebra über Alpha",
      reihenfolge({"Zebra": 1}) == ["Zebra", "Alpha", "Beta"], str(reihenfolge({"Zebra": 1})))
# Der entscheidende Fall: Eine Position darf ein ausverkauftes Produkt NICHT
# nach oben holen. Sonst müsste man beim Draufschauen jedes Mal die ganze Liste
# absuchen, ob überhaupt etwas grün ist — genau das soll das Dashboard abnehmen.
check("… aber niemals über die Verfügbarkeit hinweg",
      reihenfolge({"Beta": 1}) == ["Alpha", "Zebra", "Beta"], str(reihenfolge({"Beta": 1})))
check("kaputte Position wird ignoriert statt zu krachen",
      reihenfolge({"Zebra": "oben"}) == ["Alpha", "Zebra", "Beta"])

# Das Dashboard bekommt seine Aliase von aussen (main.py) uebergeben, die
# Stats-Karte holt sie sich selbst aus cfg. Wurden sie VOR dem Merge der
# Command-Produkte eingesammelt, kannte die Liste nur config.yml — und das
# Dashboard zeigte weiter den langen Originalwortlaut, waehrend die Stats-Karte
# direkt daneben schon den kurzen zeigte. Genau so ist es passiert.
merged = product_watch.merge_products({"products": []}, {"products": {"k": {
    "url": PROD, "name": LANG, "variants": [LANG], "label": "Azelaic 20% 30g"}}})
nach_merge = variant_labels({"products": merged})
check("Aliase erst NACH dem Merge einsammeln", nach_merge == {LANG: "Azelaic 20% 30g"},
      str(nach_merge))

karte = build_dashboard_embed([{
    "variant": LANG, "product_name": "Azelaic 20% 30g", "in_stock": True, "found": True,
    "price": "10", "product_url": "https://x"}], labels=nach_merge)["description"]
check("… dann zeigt das Dashboard den kurzen Namen",
      "Azelaic 20% 30g" in karte and LANG not in karte, karte)

vorher = variant_labels({"products": []})   # so sah es vor dem Fix aus
karte_alt = build_dashboard_embed([{
    "variant": LANG, "product_name": "Azelaic 20% 30g", "in_stock": True, "found": True,
    "price": "10", "product_url": "https://x"}], labels=vorher)["description"]
check("… und ohne die Aliase eben nicht (der alte Fehler)", LANG in karte_alt)

# --------------------------------------------------------------------------
section("/product rename für ALLE Produkte")
# --------------------------------------------------------------------------
from src.embeds import dashboard_variants  # noqa: E402

# Der springende Punkt: Roaccutane steht nur in config.yml. Haenge das
# Umbenennen an einem Produkteintrag im Gist, waere genau das unerreichbar.
FEST = "Roaccutane 20 mg 30 Roche"
cfg_fest = {"products": [{"name": "Roaccutane 20 mg (Roche)", "url": "https://x/product/r/",
                          "watch_variants": [FEST],
                          "variant_labels": {FEST: "Roaccutane 20 mg 30x"}}]}

aus_config = variant_labels(cfg_fest)
check("config.yml liefert seinen eigenen Alias", aus_config == {FEST: "Roaccutane 20 mg 30x"})

# So legt main() die beiden uebereinander.
zusammen = dict(aus_config)
zusammen.update(commands.product_labels({"labels": {FEST: "Roaccutane 30x"}}))
check("Discord schlaegt config.yml", zusammen == {FEST: "Roaccutane 30x"}, str(zusammen))

lage = [{"variant": FEST, "product_name": "Roaccutane 20 mg (Roche)", "in_stock": True,
         "found": True, "price": "39", "product_url": "https://x"}]
karte = build_dashboard_embed(lage, labels=zusammen)["description"]
check("… und das Dashboard zeigt den neuen Namen",
      "Roaccutane 30x" in karte and FEST not in karte, karte)

# Das Autocomplete des Workers speist sich hieraus: sichtbarer Name als
# Vorschlag, Match-String als gespeicherter Wert.
zeilen = dashboard_variants(lage, zusammen)
check("Zeilenliste: sichtbar + Schluessel",
      zeilen == [{"key": FEST, "label": "Roaccutane 30x"}], str(zeilen))

check("leerer Name wird ignoriert statt gespeichert",
      commands.product_labels({"labels": {FEST: "   "}}) == {})

# --------------------------------------------------------------------------
section("Konto schaltet sich nach der Zustellung selbst ab")
# --------------------------------------------------------------------------
# Einschalten heisst "ich habe bestellt", nicht "ab jetzt fuer immer". Sonst
# muesste man daran denken, es zurueckzunehmen — und genau daran denkt niemand.
fertig = {"orders": {"1": {"status": "completed", "tracking_registered": True}},
          "_initialized": True, "_was_enabled": True, "_settled_runs": 1}

# EIN erledigter Abruf reicht nicht. Man schaltet ein, WEIL man bestellt hat —
# im Shop steht die Bestellung dann aber oft noch gar nicht, und das Konto
# schliefe sofort wieder ein, ohne sie je gesehen zu haben.
with Lauf(cmds={"enabled": {"a": "on"}},
          state={"enabled": {}, "accounts": {"a": dict(fertig, _settled_runs=0)}}) as l:
    l.run(CFG)
    check("erster erledigter Abruf → noch NICHT abschalten",
          not l.state["accounts"]["a"].get("_auto_off"),
          str(l.state["accounts"]["a"].get("_settled_runs")))

with Lauf(cmds={"enabled": {"a": "on"}},
          state={"enabled": {}, "accounts": {"a": dict(fertig)}}) as l:
    l.run(CFG)
    check("zweiter → Konto ruht", l.state["accounts"]["a"].get("_auto_off") is True,
          str(l.state["accounts"]["a"].get("_auto_off")))
    check("… und sagt es als Karte", any("ruht" in (k.get("description") or "") for k in l.karten),
          str([k.get("title") for k in l.karten]))

# Der springende Punkt: Der Discord-Wunsch steht weiter auf "on" — der Bot kann
# commands.json nicht zurueckschreiben, die gehoert dem Worker. Ohne `_auto_off`
# wuerde der Wunsch das Abschalten bei jedem Lauf wieder ueberstimmen.
ruht = dict(fertig, _auto_off=True)
with Lauf(cmds={"enabled": {"a": "on"}},
          state={"enabled": {}, "accounts": {"a": dict(ruht)}}) as l:
    l.run(CFG)
    check("Wunsch bleibt 'on', trotzdem kein Login mehr", l.geprueft == [], str(l.geprueft))
    check("… und die Karte kommt nicht nochmal", l.karten == [], str(len(l.karten)))

# Und es muss sich wieder einschalten lassen. Der Wechsel aus→an hebt das
# Auto-Aus auf — sonst waere das Konto endgueltig tot.
with Lauf(cmds={"enabled": {"a": "on"}},
          state={"enabled": {}, "accounts": {"a": dict(ruht, _was_enabled=False)}}) as l:
    l.run(CFG)
    check("/account enable weckt es wieder", l.geprueft == ["haupt@x.de"], str(l.geprueft))
    check("… und raeumt das Auto-Aus weg", not l.state["accounts"]["a"].get("_auto_off"))

# Eine offene Bestellung haelt es wach.
with Lauf(cmds={"enabled": {"a": "on"}},
          state={"enabled": {}, "accounts": {"a": dict(fertig, orders={
              "1": {"status": "completed", "tracking_registered": True},
              "2": {"status": "processing"}})}}) as l:
    l.run(CFG)
    check("offene Bestellung → bleibt an", not l.state["accounts"]["a"].get("_auto_off"))

# Und ein noch nicht eingesammelter Tracking-Link ebenso: "completed" heisst
# nicht, dass der Link schon da ist — nach dem Abschalten kaeme niemand mehr ran.
with Lauf(cmds={"enabled": {"a": "on"}},
          state={"enabled": {}, "accounts": {"a": dict(fertig, orders={
              "1": {"status": "completed"}})}}) as l:
    l.run(CFG)
    check("Tracking noch nicht eingesammelt → bleibt an",
          not l.state["accounts"]["a"].get("_auto_off"))

# Laufende Sendung dieses Kontos ebenfalls. Verfolgt wird sie zwar ohne Login
# weiter — "aus" soll aber heissen, was es aussagt.
with Lauf(cmds={"enabled": {"a": "on"}},
          state={"enabled": {}, "accounts": {"a": dict(fertig)},
                 "auto_tracking": {"haupt #1": {"url": "https://h", "account": "a"}},
                 "manual_tracking_state": {"haupt #1": {"status": "Sendung ist unterwegs"}}}) as l:
    l.run(CFG)
    check("Paket noch unterwegs → bleibt an", not l.state["accounts"]["a"].get("_auto_off"))

with Lauf(cmds={"enabled": {"a": "on"}},
          state={"enabled": {}, "accounts": {"a": dict(fertig)},
                 "auto_tracking": {"haupt #1": {"url": "https://h", "account": "a"}},
                 "manual_tracking_state": {"haupt #1": {"status": "Sendung wurde zugestellt"}}}) as l:
    l.run(CFG)
    check("zugestellt → jetzt ruht es", l.state["accounts"]["a"].get("_auto_off") is True)

# Kommt zwischendrin wieder etwas rein, faengt der Zaehler von vorn an — sonst
# koennte ein halbes Jahr alter Ruhestand mitten in einer laufenden Bestellung
# mit einem einzigen weiteren Abruf zuschlagen.
with Lauf(cmds={"enabled": {"a": "on"}},
          state={"enabled": {}, "accounts": {"a": dict(fertig, orders={
              "1": {"status": "processing"}})}}) as l:
    l.run(CFG)
    check("wieder etwas offen → Zaehler zurueck auf null",
          not l.state["accounts"]["a"].get("_settled_runs"),
          str(l.state["accounts"]["a"].get("_settled_runs")))

# Ein gescheiterter Abruf darf NICHT abschalten: Der Stand von eben ist dann
# womoeglich unvollstaendig, und ein Netzfehler ist kein "alles erledigt".
with Lauf(cmds={"enabled": {"a": "on"}},
          state={"enabled": {}, "accounts": {"a": dict(fertig)}}, login_ok=False) as l:
    l.run(CFG)
    check("Abruf gescheitert → NICHT abschalten", not l.state["accounts"]["a"].get("_auto_off"))

# --------------------------------------------------------------------------
section("Karteileichen im Kontostand")
# --------------------------------------------------------------------------
# `/account remove` loescht den Eintrag in commands.json, kommt aber an
# order-state.json nicht heran — der Worker darf sie bewusst nicht anfassen.
# Der Rest blieb deshalb fuer immer in `/account list` stehen, unter seinem
# rohen Schluessel ("s3"), ohne je wieder geprueft zu werden.
leiche = {"enabled": {"a": "on", "s3": "on"},
          "accounts": {"a": {"last_check_at": "2026-08-10T00:00:00+00:00"},
                       "s3": {"orders": {}}}}
with Lauf(cmds={"enabled": {"a": "on"}}, state=dict(leiche, accounts=dict(leiche["accounts"]))) as l:
    l.run(CFG)
    check("entferntes Konto verschwindet aus dem Stand",
          "s3" not in l.state["accounts"], str(list(l.state["accounts"])))
    check("… samt seinem An/Aus-Schalter", "s3" not in l.state["enabled"], str(l.state["enabled"]))
    check("… und das echte Konto bleibt", "a" in l.state["accounts"])

# Die Absicherung, auf die es ankommt: Ist commands.json gerade nicht lesbar,
# kommt {} zurueck — dann saehen ALLE selbst hinterlegten Konten wie Leichen
# aus, und ein Aussetzer beim Lesen loeschte ihren ganzen Bestellverlauf.
with Lauf(cmds={}, state=dict(leiche, accounts=dict(leiche["accounts"]))) as l:
    l.run(CFG)
    check("commands.json unlesbar → NICHTS wird geloescht",
          "s3" in l.state["accounts"], str(list(l.state["accounts"])))

# Ein Konto aus config.yml ohne Secrets (wie mave) ist keine Leiche — es steht
# ja weiter in der Datei. Es faellt nur bei der Secret-Pruefung raus.
CFG_MAVE = {"orders": dict(CFG["orders"], accounts=CFG["orders"]["accounts"] + [
    {"name": "b", "label": "mave", "username_env": "BG_USERNAME_2", "password_env": "BG_PASSWORD_2"}])}
with Lauf(cmds={"enabled": {"a": "on"}},
          state={"enabled": {"a": "on"}, "accounts": {"a": {}, "b": {}}}) as l:
    l.run(CFG_MAVE)
    check("Konto aus config.yml ohne Secrets bleibt stehen", "b" in l.state["accounts"])

# --------------------------------------------------------------------------
section("Steht der Login noch?")
# --------------------------------------------------------------------------
# Vorher wurde `login_ok` NUR bei der Erstpruefung gesetzt. `/account list`
# zeigte danach ewig "geprueft vor 5 min" — gleich aussehend, ob der Login
# steht oder das Passwort seit Wochen falsch ist.
with Lauf(cmds={"enabled": {"a": "on"}}, state={"enabled": {}, "accounts": {}}) as l:
    l.run(CFG)
    check("geglueckter Abruf haelt das fest", l.state["accounts"]["a"].get("login_ok") is True,
          str(l.state["accounts"]["a"]))

with Lauf(cmds={"enabled": {"a": "on"}},
          state={"enabled": {}, "accounts": {"a": {"login_ok": True}}}, login_ok=False) as l:
    l.run(CFG)
    check("gescheiterter Abruf auch — bei JEDEM Lauf, nicht nur beim ersten",
          l.state["accounts"]["a"].get("login_ok") is False, str(l.state["accounts"]["a"]))

# --------------------------------------------------------------------------
section("Wohin Deploy-Karten und Fehler-Reports gehen")
# --------------------------------------------------------------------------
# Der Webhook dafuer zeigte monatelang auf einen Channel, den niemand sah — und
# das konnte niemand merken: Discord quittiert einen Webhook-POST auch dann mit
# 204, wenn dort keiner mitliest. Jetzt gilt zuerst der Channel, in dem zuletzt
# ein Command lief; den traegt der Worker ins Gist ein.
check("kein Eintrag → kein Channel", commands.updates_channel({}) == "")
check("Channel aus dem Gist",
      commands.updates_channel({"guilds": {"g1": {"channel_id": "1234567890123456789"}}})
      == "1234567890123456789")
check("etwas anderes als eine ID wird nicht geglaubt",
      commands.updates_channel({"guilds": {"g1": {"channel_id": "bg-oat"}}}) == "")

# Bei mehreren Servern gewinnt der juengste Eintrag — "wo zuletzt jemand war"
# ist die einzige Angabe, die hier ohne Rueckfrage stimmen kann.
viele = {"guilds": {
    "alt": {"channel_id": "111", "channel_at": "2026-01-01T00:00:00Z"},
    "neu": {"channel_id": "222", "channel_at": "2026-08-08T00:00:00Z"},
}}
check("juengster Eintrag gewinnt", commands.updates_channel(viele) == "222")


class FakeZiel(notify.UpdateTarget):
    """Zaehlt, welchen Weg `send` genommen haette."""

    def __init__(self, *a, bot_ok=True, **k):
        super().__init__(*a, **k)
        self.bot_ok, self.wege = bot_ok, []


_orig_bot, _orig_wh = notify.send_bot_message, notify.send_update_announcement
try:
    notify.send_bot_message = lambda t, c, e: (ziel.wege.append("bot"), ziel.bot_ok)[1]
    notify.send_update_announcement = lambda w, e: (ziel.wege.append("webhook"), bool(w))[1]

    ziel = FakeZiel(webhook="https://wh", bot_token="tok", channel_id="123")
    check("mit Channel: der Bot schreibt, der Webhook bleibt aussen vor",
          ziel.send({}) and ziel.wege == ["bot"], str(ziel.wege))

    ziel = FakeZiel(webhook="https://wh", bot_token="tok", channel_id="123", bot_ok=False)
    check("darf der Bot dort nicht schreiben → Webhook als Rueckfallebene",
          ziel.send({}) and ziel.wege == ["bot", "webhook"], str(ziel.wege))

    ziel = FakeZiel(webhook="https://wh")
    check("ohne Bot-Token bleibt alles wie vorher",
          ziel.send({}) and ziel.wege == ["webhook"], str(ziel.wege))

    ziel = FakeZiel(bot_token="tok")  # Token da, aber noch nie ein Command
    check("kein Ziel → der Aufrufer merkt es, statt ins Leere zu senden", not bool(ziel))
    check("… und mit Channel ist es eines", bool(FakeZiel(bot_token="tok", channel_id="1")))
finally:
    notify.send_bot_message, notify.send_update_announcement = _orig_bot, _orig_wh

print(f"\n{failed} FEHLER" if failed else "\nALLES GRÜN")
sys.exit(1 if failed else 0)
