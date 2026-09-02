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

# Ein Konto aus config.yml ohne Secrets ist keine Leiche — es steht
# ja weiter in der Datei. Es faellt nur bei der Secret-Pruefung raus.
CFG_ZWEITKONTO = {"orders": dict(CFG["orders"], accounts=CFG["orders"]["accounts"] + [
    {"name": "b", "label": "zweit", "username_env": "BG_USERNAME_2", "password_env": "BG_PASSWORD_2"}])}
with Lauf(cmds={"enabled": {"a": "on"}},
          state={"enabled": {"a": "on"}, "accounts": {"a": {}, "b": {}}}) as l:
    l.run(CFG_ZWEITKONTO)
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
section("Gist: nie schreiben, was nicht lesbar war")
# --------------------------------------------------------------------------
# Ein gescheiterter Abruf gibt {} zurueck — fuer den Aufrufer sieht das aus wie
# ein leerer Stand, und er baut darauf einen neuen auf. Ohne Deckel haette ein
# einziger 503 von GitHub Bestellungen, Cookies und verfolgte Sendungen ersetzt.
from src import gist as gistmod  # noqa: E402


class _Antwort:
    status_code = 200
    def __init__(self, daten=None, kaputt=False):
        self._d, self._kaputt = daten or {}, kaputt
    def raise_for_status(self):
        if self._kaputt:
            raise requests.RequestException("503 Service Unavailable")
    def json(self):
        return self._d


import requests  # noqa: E402

_orig_get, _orig_patch = requests.get, requests.patch
geschrieben: list = []
try:
    requests.patch = lambda *a, **k: (geschrieben.append(k.get("json")), _Antwort())[1]

    requests.get = lambda *a, **k: _Antwort(kaputt=True)
    gistmod.reset()
    gistmod.read("t", "g", "order-state.json")
    ok_block = gistmod.write("t", "g", "order-state.json", {"leer": True})
    check("Lesen gescheitert → Schreiben blockiert", ok_block is False and not geschrieben, f"{len(geschrieben)} Schreibvorgänge")

    requests.get = lambda *a, **k: _Antwort({"files": {"order-state.json": {"content": '{"a":1}'}}})
    gistmod.reset(); geschrieben.clear()
    check("Lesen ok → Schreiben erlaubt", gistmod.write("t", "g", "order-state.json", {"a": 2}) is True)

    # Erstanlage: erreichbar, aber leer. Muss gehen, sonst kaeme nie etwas rein.
    requests.get = lambda *a, **k: _Antwort({"files": {}})
    gistmod.reset()
    check("leeres Gist → Schreiben erlaubt", gistmod.write("t", "g", "order-state.json", {"neu": 1}) is True)

    # Ein Abruf fuer beide Dateien, und ein Schreibvorgang ist danach sichtbar.
    aufrufe = {"n": 0}
    def zaehl(*a, **k):
        aufrufe["n"] += 1
        return _Antwort({"files": {"order-state.json": {"content": '{"a":1}'},
                                   "commands.json": {"content": '{"b":2}'}}})
    requests.get = zaehl
    gistmod.reset()
    for _ in range(4):
        gistmod.read("t", "g", "order-state.json")
        gistmod.read("t", "g", "commands.json")
    check("8 Lesevorgänge → 1 HTTP-Abruf", aufrufe["n"] == 1, f"{aufrufe['n']} Abrufe")
    gistmod.write("t", "g", "order-state.json", {"a": 99})
    check("Schreiben ist danach sichtbar", gistmod.read("t", "g", "order-state.json") == {"a": 99})
    check("andere Datei unberührt", gistmod.read("t", "g", "commands.json") == {"b": 2})
finally:
    requests.get, requests.patch = _orig_get, _orig_patch
    gistmod.reset()

# --------------------------------------------------------------------------
section("Discord-Grenzen aller Karten")
# --------------------------------------------------------------------------
# Discord kuerzt nicht, es lehnt die ganze Nachricht mit HTTP 400 ab — und eine
# Karte, die in place editiert wird, bleibt dann einfach auf dem alten Stand
# stehen, ohne dass irgendwo etwas aufschlaegt. Genau so ist /panel schon einmal
# liegengeblieben. Deshalb hier absichtlich unrealistisch viel Material.
from src import embeds  # noqa: E402

LIMITS = {"title": 256, "description": 4096}


def embed_ok(name, e):
    fehler = []
    for feld, grenze in LIMITS.items():
        if len(e.get(feld) or "") > grenze:
            fehler.append(f"{feld}={len(e[feld])}>{grenze}")
    for f in e.get("fields") or []:
        if len(f.get("value") or "") > 1024:
            fehler.append(f"field '{f['name'][:20]}'={len(f['value'])}>1024")
        if len(f.get("name") or "") > 256:
            fehler.append("field-name>256")
    if len(e.get("fields") or []) > 25:
        fehler.append("mehr als 25 fields")
    gesamt = (len(e.get("title") or "") + len(e.get("description") or "")
              + len((e.get("footer") or {}).get("text") or "")
              + sum(len(f.get("name") or "") + len(f.get("value") or "") for f in e.get("fields") or []))
    if gesamt > 6000:
        fehler.append(f"gesamt={gesamt}>6000")
    check(name, not fehler, ", ".join(fehler) or f"{gesamt} Zeichen")


LANG = "Sehr langer Produktname der wirklich kein Ende nehmen will " * 3
viele = [{"name": f"{LANG} {i}", "variant": f"{LANG} Variante {i}", "found": True,
          "in_stock": i % 2 == 0, "price": "€ 1.234,56",
          "product_url": f"https://bgpharmadrugs.to/product/x{i}/",
          "deep_link": f"https://bgpharmadrugs.to/product/x{i}/?a=b"} for i in range(200)]

embed_ok("Dashboard mit 200 Varianten", embeds.build_dashboard_embed(viele))

# Die Stats-Karte liest ihre Produkte aus der CONFIG, den Zustand dazu aus dem
# State. Wer nur eines von beiden füllt, testet nichts — genau das ist mir hier
# beim ersten Anlauf passiert: 87 Zeichen Ausgabe und trotzdem „ok".
cfg_viel = {"products": [{"name": f"{LANG} {i}", "url": f"https://x/{i}",
                          "watch_variants": [f"{LANG} Variante {i}"]} for i in range(200)]}
state_viel = {"products": {f"https://x/{i}": {f"{LANG} Variante {i}": {
    "in_stock": True, "price": "€ 9,99", "price_history": [9.99, 12.5, 8.0] * 12, "found": True,
    "lowest_price": "€ 8,00", "highest_price": "€ 19,99",
    "oos_periods": [{"start": "2026-01-01T00:00:00+00:00", "end": "2026-01-05T00:00:00+00:00"}],
}} for i in range(200)}, "bot_stats": {"total_checks": 99999, "total_restocks": 7,
                                       "first_check_at": "2026-01-01T00:00:00+00:00"}}
stats = embeds.build_stats_embed(cfg_viel, state_viel)
check("Stats-Test greift wirklich", len(stats["description"]) > 500, f"{len(stats['description'])} Zeichen")
embed_ok("Stats mit 200 Produkten", stats)

embed_ok("Sendung mit 50 Ereignissen", embeds.build_shipment_embed(
    LANG[:24],
    {"number": "H" + "1" * 20, "summary": LANG,
     "details": {f"Feld {i}": LANG for i in range(10)},
     "events": [{"date": "01.01.2026", "time": "12:00", "text": LANG} for _ in range(50)]},
    [{"date": "01.01.2026", "time": "12:00", "text": LANG} for _ in range(50)],
    "https://tracking.hermesworld.com/?TrackID=" + "1" * 30, first=True))

embed_ok("Produkt-Scan mit 200 Varianten", embeds.build_product_scan_embed(
    "https://bgpharmadrugs.to/product/x/",
    {"title": LANG, "simple": False, "variants": [f"{LANG} {i}" for i in range(200)]}))

embed_ok("Bestellkarte", embeds.build_order_status_embed(
    {"order_id": "1", "status": "processing", "status_text": LANG, "url": "https://x"},
    fresh=True, items=[LANG] * 40, owner=LANG[:20]))

embed_ok("Login-Fehlschlag", embeds.build_account_check_embed(LANG[:24], False, LANG * 5))
embed_ok("Fehler-Report", embeds.build_error_embed([LANG] * 40))
embed_ok("Restock", embeds.build_restock_embed(
    {"name": LANG, "variant": LANG, "price": "€ 1,00", "deep_link": "https://x", "out_since": ""}))

# --------------------------------------------------------------------------
section("Wieder-da-Melder (retail)")
# --------------------------------------------------------------------------
from src import retail, retail_watch  # noqa: E402

# Genau so sieht es auf crocs.eu aus — nachgemessen an beiden echten Seiten.
DA = """<script type="application/ld+json">
{"@type":"Product","name":"Dinoco Clog","sku":"213582",
 "offers":{"availability":"https://schema.org/InStock","price":74.99,"priceCurrency":"EUR"}}
</script>"""
WEG = """<script type="application/ld+json">
{"@type":"Product","name":"McQueen Clog","sku":"205759","offers":{}}</script>"""

check("verfuegbar wird erkannt", retail.parse(DA)["in_stock"] is True)
check("… mit Preis und Waehrung", (retail.parse(DA)["price"], retail.parse(DA)["currency"]) == ("74.99", "EUR"))
check("ausverkauft: kein availability → nicht verfuegbar", retail.parse(WEG)["in_stock"] is False)
check("… aber die Seite gilt als gefunden", retail.parse(WEG)["found"] is True)

# Die Woerter "Sold Out"/"Notify Me" stehen im Textbundle JEDER Seite, egal wie
# der Zustand ist. Wer danach sucht, misst die Uebersetzung statt den Bestand.
check("Textschnipsel taeuschen den Parser nicht",
      retail.parse(DA + "<div>Sold Out</div><span>Notify Me</span>")["in_stock"] is True)

# @graph und Angebotslisten kommen in freier Wildbahn vor.
check("@graph wird ausgepackt", retail.parse(
    '<script type="application/ld+json">{"@graph":[{"@type":"WebSite"},'
    '{"@type":"Product","name":"X","offers":{"availability":"InStock","price":5}}]}</script>'
)["in_stock"] is True)
check("aus mehreren Angeboten zaehlt das kaufbare", retail.parse(
    '<script type="application/ld+json">{"@type":"Product","name":"X","offers":['
    '{"availability":"https://schema.org/OutOfStock"},'
    '{"availability":"https://schema.org/InStock","price":9.5}]}</script>'
)["in_stock"] is True)
check("PreOrder gilt NICHT als kaufbar", retail.parse(
    '<script type="application/ld+json">{"@type":"Product","name":"X",'
    '"offers":{"availability":"https://schema.org/PreOrder","price":1}}</script>'
)["in_stock"] is False)

CFG_RETAIL = {"retail": {"items": [
    {"name": "McQueen", "emoji": "🏎️", "urls": ["https://a/x", "https://b/x"]}]}}

DA = {"found": True, "in_stock": True, "price": "74.99", "currency": "EUR",
      "name": "McQueen Clog", "sku": "205759", "error": ""}
WEG = dict(DA, in_stock=False, price="", currency="")
KAPUTT = dict(WEG, found=False, error="HTTP 503")


def lauf(state, antworten):
    """Ein retail-Durchgang. `antworten` bildet URL → Antwort ab."""
    orig = retail.check
    retail.check = lambda url, timeout=20: dict(antworten[url], url=url)
    try:
        return retail_watch.check_items(CFG_RETAIL, state)
    finally:
        retail.check = orig


ALLE_WEG = {"https://a/x": WEG, "https://b/x": WEG}
ALLE_DA = {"https://a/x": DA, "https://b/x": DA}

# Erstsichtung ist still — sonst gaebe es beim Eintragen sofort einen Alarm
# fuer etwas, das man gerade selbst hinzugefuegt hat.
st = {}
_, r = lauf(st, ALLE_DA)
check("Erstsichtung meldet nichts", r == [], str(r))
check("… merkt sich aber den Zustand", st["retail"]["McQueen"]["in_stock"] is True)

st = {}
lauf(st, ALLE_WEG)
_, r = lauf(st, ALLE_DA)
check("weg → da meldet genau EINEN Restock", len(r) == 1 and r[0]["name"] == "McQueen", str(r))
check("… mit Preis fuer die Karte", r[0]["price"] == "74.99")

_, r = lauf(st, ALLE_DA)
check("bleibt da → keine zweite Meldung", r == [], str(r))
_, r = lauf(st, ALLE_WEG)
check("wieder weg → still", r == [], str(r))

# Der Punkt an mehreren Quellen: EINE reicht, und die Karte zeigt genau die.
# Zwei Karten fuer denselben Artikel waeren zweimal dieselbe Nachricht.
st = {}
lauf(st, ALLE_WEG)
_, r = lauf(st, {"https://a/x": WEG, "https://b/x": DA})
check("nur der zweite Shop hat sie → trotzdem eine Meldung", len(r) == 1, str(r))
check("… und sie verlinkt genau diesen Shop", r[0]["url"] == "https://b/x", str(r[0]))

# Der Fall, der sonst still Fehlalarme baut: Ist KEINE Quelle lesbar, darf das
# nicht als "ausverkauft" gelten — sonst meldet der naechste geglueckte Abruf
# einen Restock, obwohl sich nie etwas geaendert hat.
st = {}
lauf(st, ALLE_DA)
_, r = lauf(st, {"https://a/x": KAPUTT, "https://b/x": KAPUTT})
check("keine Quelle lesbar → Zustand bleibt", st["retail"]["McQueen"]["in_stock"] is True)
_, r = lauf(st, ALLE_DA)
check("… und danach KEIN Fehlalarm", r == [], str(r))

# Eine kaputte Quelle darf die heile nicht entwerten.
st = {}
lauf(st, ALLE_WEG)
_, r = lauf(st, {"https://a/x": KAPUTT, "https://b/x": DA})
check("eine Quelle kaputt, die andere hat sie → Meldung kommt", len(r) == 1, str(r))

print(f"\n{failed} FEHLER" if failed else "\nALLES GRÜN")
sys.exit(1 if failed else 0)
