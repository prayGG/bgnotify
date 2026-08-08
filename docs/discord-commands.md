# bgnotify · Discord-Commands — Übergabe

Alles, was ein neuer Bearbeiter braucht, um an diesem Umbau weiterzumachen:
Kontext, Plan, aktueller Stand, nächste Handgriffe. Selbsttragend — kein
Chatverlauf nötig.

**Ziel:** Was heute Handarbeit im privaten Gist ist (Konto an/aus schalten,
Tracking-Links eintragen), soll per Slash-Command gehen. Harte Bedingung des
Betreibers: **muss dauerhaft kostenlos sein.**

---

## 1 · Wie bgnotify heute funktioniert

Ein Python-Bot, der auf GitHub Actions läuft — angestoßen von einem externen
Cronjob alle 10 Minuten (in den Workflows steht bewusst kein `schedule:`, alle
drei sind `workflow_dispatch`).

```
src/
├── main.py          Orchestrator, ruft die Watcher der Reihe nach
├── bgpharma.py      WooCommerce-Produkte scrapen
├── orders.py        Kundenkonto-Login (Playwright), Order-Parsing, Gist-I/O
├── hermes.py        Sendungsverfolgung scrapen
├── stock_watch.py   Produkt-Checks
├── order_watch.py   Bestellungen diffen, Account-Staggering
├── hermes_watch.py  Sendungen verfolgen
├── embeds.py        alle Discord-Embeds (nur Rendering, kein State)
└── notify.py        Webhook-Versand
```

### Wo was liegt — das ist die wichtigste Regel

| Ort | Inhalt | Sichtbarkeit |
|---|---|---|
| Repo (`config.yml`, `state.json`) | Produkte, Intervalle, Preis-History, Message-IDs | **öffentlich** |
| privates Gist (`order-state.json`) | Bestellungen, Cookies, `enabled`-Schalter, verfolgte Sendungen | privat |
| GitHub-Secrets | Webhook-URLs, BG-Zugangsdaten, Discord-IDs, Gist-Token | write-only |

**Nie** Zugangsdaten, Webhook-URLs oder Discord-IDs ins Repo. `config.yml`
enthält ausschließlich die *Namen* von Secrets, nie deren Werte.

### Takte

| Was | Intervall | Quelle |
|---|---|---|
| Bot-Lauf | 10 min | externer Cronjob |
| Order-Check, Bestellung offen | 240 min | `orders.check_interval_minutes` |
| Order-Check, nichts offen | 1440 min | `orders.idle_interval_minutes` |
| Sendungsabfrage | 15 min → effektiv 20 | `tracking.check_interval_minutes` |

Das 10-Minuten-Raster quantisiert die Sendungsabfrage: Mit ±10% Jitter ist jeder
Wert zwischen 10 und 20 in der Praxis genau 20 Minuten. Für jeden Lauf müsste
man auf 9 runter.

Pro Lauf wird höchstens **ein** Konto eingeloggt (Staggering) und
`tracking.max_per_run` Sendungen abgefragt (Standard 2). Das ist Absicht: Es soll nicht so aussehen, als hinge ein
Automat am Kundenkonto.

---

## 2 · Was heute schon fertig wurde (gemerged auf `main`)

Vorgeschichte, damit klar ist, warum es `auto_tracking` gibt.

**Das Problem:** Fand `order_watch` bei einer Bestellung einen Tracking-Link,
postete es die Karte „Tracking ist da" — und das war das Ende. `hermes_watch`,
das den Sendungsverlauf tatsächlich scrapt, las ausschließlich `manual_tracking`
aus dem Gist, also nur von Hand eingetragene Links. Die beiden Watcher kannten
sich nicht.

**Behoben durch** (PR #2 und #3, beide gemerged):

- `order_watch` legt gefundene Links im Gist unter `auto_tracking` ab;
  `hermes_watch` liest den Block zusätzlich zu `manual_tracking`. Bei gleichem
  Label gewinnt der Handeintrag.
- `check_shipments` läuft in `main.py` direkt nach `check_orders` — die Sendung
  wird also noch im selben Lauf mitgenommen.
- Ist die Sendung zugestellt, fliegen Eintrag und Stand raus.
  `tracking_registered` verhindert ein Wieder-Eintragen.
- Einmalige Nachrüstung für Bestellungen von vor der Änderung, begrenzt auf
  30 Tage. Steht sie an, ist das Konto sofort fällig statt erst im Idle-Takt —
  gedeckelt auf genau einen Lauf über `_backfill_done`.
- Bestellkarten erklären ihren Status in einer Zeile Klartext („Completed" =
  bei BG fertig und rausgeschickt, **nicht** zugestellt).
- Alle Karten zeigen oben nur die Konto-Bezeichnung (`pray`, `mave`). Die
  Bestellnummer taucht nirgends auf: Konten ohne hinterlegte BG-Zugänge laufen
  nur über `manual_tracking` und haben nie eine.

---

## 3 · Der Plan für die Commands

### 3.1 Warum kein klassischer Bot

Der Reflex wäre ein Gateway-Bot mit dauerhafter WebSocket-Verbindung — das
braucht einen laufenden Prozess und damit einen gemieteten Server.

Discord kann Slash-Commands aber auch per **HTTP-Interactions-Endpoint**
ausliefern: Man hinterlegt eine HTTPS-URL, Discord schickt jeden Command als
POST dorthin. Zustandslos, passt auf eine Serverless-Funktion.
**Cloudflare Workers**, Free-Tarif: 100.000 Requests/Tag, keine Kreditkarte.
Verbrauch hier: eine Handvoll pro Tag.

### 3.2 Trennung nach Richtung, nicht nach Technik

Nicht alles auf Bot umstellen. Webhooks dürfen pro Nachricht Name und Avatar
setzen — davon lebt das Setup mit `bgnotify · orders`, `· meso`, `· updates`.
Ein echter Bot postet immer als er selbst; die drei Absender wären weg.

```
Discord ──Command──▶ Worker ──schreibt──▶ Gist: commands.json
                                              │
Discord ◀──Webhook── Actions-Bot ◀──liest────┘
                          └──schreibt──▶ Gist: order-state.json
```

**Zwei getrennte Gist-Dateien, und das ist keine Kosmetik.** Der Bot macht
laden → ändern → speichern. Schreibt ein Command genau dazwischen in dieselbe
Datei, wäre die Command-Änderung weg. Getrennte Dateien schließen das baulich
aus.

### 3.3 Zugangsdaten ohne Weitergabe

Heute müsste ein zweiter Nutzer sein BG-Passwort an den Betreiber schicken,
damit der es als GitHub-Secret einträgt. Das soll `/account add` abschaffen.

Ablauf: Der Command öffnet ein **Modal** — ein Formularfenster in Discord, kein
Command-Parameter. Ein Passwort als Option wäre schlechtes Handwerk: Es stünde
im Eingabefeld, in der Command-Historie des Clients und auf jedem Screenshare.
Der Worker verschlüsselt die Werte sofort mit dem öffentlichen Schlüssel des
Repos (libsodium sealed box, z.B. via tweetnacl) und legt sie per API als
**GitHub-Secret** ab. Ins Gist kommen nur Anzeigename und Slot-Nummer.

```
Nutzer (Modal) ──▶ Worker (verschlüsselt) ──▶ Repo-Secret ──▶ Bot-Lauf (env)
                        │
                        └──▶ Gist: nur Name + Slot, kein Passwort
```

> **Ehrliche Grenze, die niemandem verschwiegen werden darf:** Der Bot muss
> sich mit den Daten bei BG einloggen, braucht sie also im Klartext. Wer Code
> und Secrets kontrolliert, kann sie sich jederzeit ausgeben lassen. Vertrauen
> wird nicht überflüssig, nur verlagert. Nicht als Unantastbarkeit verkaufen.

**Slots:** Ein neues Secret nützt nichts, wenn der Workflow es nicht
durchreicht — und Actions kann Secrets nicht dynamisch auflisten. Deshalb
werden in `main.yml` vorab leere Plätze verdrahtet (`BG_USERNAME_3` bis `_6`),
`/account add` belegt den nächsten freien. Harte Grenze bei vier zusätzlichen
Konten. Die Alternative `toJSON(secrets)` kippt *alle* Secrets in eine Variable,
inklusive Gist-Token — **nicht** machen.

Ob die Daten stimmen, weiß erst der nächste Lauf (Login testen heißt
Playwright). Der Command antwortet „angelegt, wird geprüft", der Bot meldet
danach *Login ok* oder *Login fehlgeschlagen*.

### 3.4 Die Commands

Namen auf Englisch. **sofort** = Worker allein, Antwort unter einer Sekunde.
**nächster Lauf** = braucht einen echten Browser, Auftrag wandert ins Gist.

| Command | Tut was | Wann |
|---|---|---|
| `/account add` | Modal für BG-Zugangsdaten → verschlüsselt ins Repo-Secret | Prüfung folgt |
| `/account list` | Konten mit Zustand: an/aus, letzter Login, offene Bestellungen | sofort |
| `/account enable` / `disable` | der `enabled`-Schalter im Gist | sofort |
| `/account remove` | Konto raus, Secrets gelöscht, Slot frei | sofort |
| `/track add` | Hermes-Link eintragen, optional Name und Ping-Ziel | sofort |
| `/track list` | was verfolgt wird, mit letztem Stand und letzter Abfrage | sofort |
| `/track remove` | Eintrag löschen, Namen per Autocomplete | sofort |
| `/product add` | URL rein, Bot liest Varianten und lässt auswählen | nächster Lauf |
| `/product list` / `remove` | Beobachtungsliste zeigen, Einträge entfernen | sofort |
| `/run` | stößt sofort einen Bot-Lauf an (`workflow_dispatch`) | sofort |
| `/status` | läuft der Bot, letzter Lauf, Konten, Webhooks | sofort |
| `/setup` | einmalig: Rolle anlegen, Rechte einrichten (nur Server-Inhaber) | sofort |

**Bewusst nicht gebaut:**

- *PlayStation-Commands* — vom Betreiber gestrichen.
- *Bestellstatus auf Zuruf* — jeder Aufruf wäre ein BG-Login. Das ganze Takt-
  und Staggering-Design existiert, damit das selten passiert. Der Status kommt
  ohnehin von allein.
- *Webhook-URLs per Command ändern* — gehört in die GitHub-Oberfläche.

### 3.5 Die Rolle

`/setup` legt per API eine Rolle `bgnotify` an (Bot braucht `Manage Roles`).
Die Prüfung passiert **im Worker**: Discord schickt bei jedem Command die Rollen
des Aufrufers mit, der Worker vergleicht mit der Rollen-ID und lehnt sonst ab.
Discords eigene Command-Berechtigungen bräuchten einen zusätzlichen
OAuth2-Flow — den spart man sich. Zusätzlich `default_member_permissions: "0"`,
damit die Commands bei allen anderen gar nicht in der Liste auftauchen.

Bootstrap: Solange die Rolle nicht existiert, darf nur der Server-Inhaber
`/setup` aufrufen.

### 3.6 „Maxed out" heißt konkret

- **Autocomplete überall** — Konten, Sendungen, Produkte, Varianten. Vorschläge
  live aus dem Gist. Nichts abtippen, keine Tippfehler, die ins Leere greifen.
- **Antworten ephemeral** (Flag 64) — Ausgaben enthalten Bestell- und
  Sendungsdaten, die müssen nicht im Channel stehenbleiben.
- **Knöpfe statt Nachfragen** — `/product add` zeigt Varianten als Auswahlmenü,
  `/account remove` fragt mit *Abbrechen / Löschen*.
- **Fehler, die etwas sagen** — „Kein Konto `mave` — meintest du `maveg`?"

---

## 4 · Reihenfolge

Nach jedem Schritt steht etwas Nachprüfbares. Riskantestes zuerst.

| # | Schritt | Fertig, wenn |
|---|---|---|
| 1 | Discord-App, Worker mit Signaturprüfung, `/ping` | `/ping` sagt „pong" |
| 2 | `/setup` + Rollencheck im Worker | `/ping` ohne Rolle wird abgelehnt |
| 3 | Nur-lesend: `/status`, `/track list`, `/account list` | zeigen dasselbe wie das Gist |
| 4 | Schreibend: `/account enable\|disable`, `/track add\|remove`, Autocomplete | `disable` lässt den nächsten Lauf aussetzen |
| 5 | `src/commands.py` — Bot arbeitet Aufträge ab, Merge in `config.py` | Auftrag im Gist wird verarbeitet und quittiert |
| 6 | `/account add` — Modal, sealed box, Slots, Login-Prüfung | zweiter Nutzer legt sein Konto selbst an, Lauf meldet *Login ok* |
| 7 | `/run` und `/product` | per Command aufgenommenes Produkt taucht im Dashboard auf |

### Was noch dazukommt

| Datei | Zweck |
|---|---|
| `worker/src/secrets.js` | Sealed-Box-Verschlüsselung für Repo-Secrets |
| `src/commands.py` | Bot arbeitet Aufträge ab (Produkt aufnehmen, Login prüfen) |
| `src/config.py` | *Änderung:* Produkte und Konten aus dem Gist anhängen |
| `.github/workflows/main.yml` | *Änderung:* die freien Konto-Slots verdrahten |

**Warum Produkte ins Gist und nicht in `config.yml`:** Die `config.yml` ist
voller Erklärkommentare — jede Zeile begründet, warum ein Match-String roh
bleibt oder ein Intervall so gewählt ist. Ein Programm, das YAML einliest und
neu schreibt, wirft die alle weg. Von Hand gepflegte Produkte bleiben in der
YAML, per Command hinzugefügte landen im Gist, beim Laden werden beide
zusammengeführt.

---

## 5 · Stand: Schritt 1 ist gebaut

Auf Branch `claude/bgnotify-hermes-tracking-dyao5d`, **noch nicht gemerged**.

```
worker/
├── src/index.js      Signaturprüfung, Routing, /ping
├── register.js       meldet Commands bei Discord an
├── test.mjs          Signaturtest ohne Deploy
├── wrangler.toml     Deploy-Konfiguration
├── package.json
└── README.md         Einrichtungsanleitung
```

Zwei Entscheidungen im Code, die nicht offensichtlich sind:

1. Geprüft wird über die **rohen Body-Bytes**, nicht über neu serialisiertes
   JSON — `JSON.stringify` liefert nicht zwingend dieselbe Byte-Folge zurück,
   die Signatur wäre wertlos.
2. Es werden **zwei Algorithmus-Schreibweisen** probiert (`Ed25519` und das
   ältere `NODE-ED25519`), weil die Workers-Runtime historisch den Altnamen
   verlangte. So überlebt der Code ein Plattform-Update.

Getestet mit echten Ed25519-Schlüsseln gegen `worker.fetch()` — sieben Fälle,
alle grün:

```bash
cd worker && node test.mjs
```

Der wichtigste Fall ist „kaputte Signatur → 401": Discord testet die
Endpoint-URL beim Eintragen genau damit und akzeptiert sie nur, wenn abgelehnt
wird. Läuft der Test durch, wird Discord die URL annehmen.

---

## 6 · Nächste Handgriffe

### Manuell (nur der Betreiber, hinter seinen Logins)

Ablauf steht ausführlich in `worker/README.md`. Kurzfassung, Reihenfolge zählt:

1. Discord-App anlegen → Application ID, Public Key, Bot Token notieren
2. `npm install`, `npx wrangler login`, `npx wrangler deploy`
3. `npx wrangler secret put DISCORD_PUBLIC_KEY`
4. **Erst jetzt** die workers.dev-URL bei Discord als *Interactions Endpoint
   URL* eintragen — vorher schlägt Discords Prüfung fehl, weil der Schlüssel
   fehlt
5. Bot einladen (Scopes `bot` + `applications.commands`, Permission
   `Manage Roles`)
6. `node register.js` mit App-ID, Bot-Token und Guild-ID

### Dann Schritt 2

`/setup` und der Rollencheck. Früh, damit alles Weitere von Anfang an geschützt
ist statt nachträglich.

---

## 7 · Regeln für den, der weitermacht

- **Kommentare auf Deutsch**, wie im ganzen Repo. Sie begründen *warum*, nicht
  *was* — dieser Stil wird erwartet und ist stellenweise die einzige
  Dokumentation einer Entscheidung.
- **Command-Namen auf Englisch**, so gewünscht.
- **Nichts Geheimes ins Repo.** Es ist öffentlich, inklusive `state.json`.
- **Nie in `order-state.json` schreiben** vom Worker aus. Nur `commands.json`.
- **Discord verlangt Antwort binnen 3 Sekunden.** Alles Langsamere sofort
  bestätigen und das Ergebnis später über den bestehenden Webhook melden.
- **Antworten ephemeral**, sobald Bestell- oder Sendungsdaten drinstehen.
- **Entwickeln auf** `claude/bgnotify-hermes-tracking-dyao5d`, pushen mit
  `git push -u origin <branch>`. PRs nur auf ausdrücklichen Wunsch.
- Token so eng wie möglich schneiden: Gist-Token nur für Gists, der Token für
  `/run` nur mit Actions-Recht auf dieses eine Repo, der für `/account add` nur
  mit Secrets-Recht.
