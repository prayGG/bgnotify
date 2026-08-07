# bgnotify · Discord-Commands

Interactions-Endpoint auf Cloudflare Workers. Nimmt Slash-Commands entgegen und
bearbeitet das private Gist.

**Meldungen postet er nicht** — Restocks, Bestellungen und Sendungen laufen
weiterhin über die Webhooks des Actions-Bots, weil die pro Nachricht Absender
und Avatar setzen dürfen (`bgnotify · orders`, `· meso`, `· updates`). Ein Bot
postet immer als er selbst, die drei Absender wären also verloren. Die einzige
Ausnahme ist die Befehlsübersicht aus `/panel`: Die ist die eigene Oberfläche
des Bots, keine Meldung.

Kein dauerlaufender Prozess: Discord schickt jeden Command als HTTPS-POST, der
Worker antwortet. Deshalb dauerhaft kostenlos (Workers-Free: 100.000
Requests/Tag, keine Kreditkarte).

## Stand

Alle sieben Schritte gebaut.

| | |
|---|---|
| lesen | `/status`, `/track list`, `/account list` |
| schreiben | `/account enable`, `/account disable`, `/track add`, `/track remove` |
| auslösen | `/run` |
| Konten selbst hinterlegen | `/account add`, `/account remove` |
| Produkte | `/product list`, `/product add`, `/product remove` |
| Rest | `/ping`, `/setup`, `/panel` |

Was schreibt, schreibt **nur** nach `commands.json` — nie nach
`order-state.json`. Der Worker legt dort einen Wunschzustand ab
(`{"enabled": {"a": "off"}}`), der Bot legt ihn beim Lesen über seinen eigenen
Stand. Ein Wunsch ist idempotent: keine Quittung nötig, doppelte Verarbeitung
schadet nicht.

Die Befehlsübersicht im Channel (`/panel`) entsteht vollständig aus
`src/catalog.js` — derselben Liste, aus der auch `register.js` die Anmeldung
bei Discord baut. Kommt ein Command dazu, ändert sich der Fingerabdruck des
Katalogs, und der nächste beliebige Command lässt die Nachricht im Hintergrund
neu zeichnen. Zwei getrennte Listen wären schon nach dem zweiten Command
auseinandergelaufen — unbemerkt, weil nichts sie widerlegt.

Ab hier ist jeder Command hinter der Rolle `bgnotify` eingesperrt. Das kam
bewusst früh: Alles Weitere fasst Bestelldaten und Zugangsdaten an, und
nachträglich abzusichern ist die Reihenfolge, in der man Lücken übersieht.

`default_member_permissions: "0"` blendet die Commands bei normalen Mitgliedern
nur **aus** — das ist Sichtbarkeit, keine Absicherung. Verbindlich prüft der
Worker, weil Discord die Rollen des Aufrufers bei jedem Command mitschickt.

## Secrets

| Secret | Wofür | Nötig ab |
|---|---|---|
| `DISCORD_PUBLIC_KEY` | Signaturprüfung eingehender Anfragen | Schritt 1 |
| `DISCORD_BOT_TOKEN` | Rolle anlegen und zuweisen (`/setup`) | Schritt 2 |
| `GIST_TOKEN` | PAT mit **ausschließlich** `gist`-Recht | Schritt 2 |
| `GIST_ID` | ID des privaten Gists | Schritt 2 |
| `GITHUB_TOKEN` | Lauf anstoßen und Secrets setzen — **fein-granular**, *Actions: read and write* **und** *Secrets: read and write* auf dieses Repo | Schritt 5 |

Setzen mit `npx wrangler secret put <NAME>`, Wert danach eingeben. Nichts davon
gehört in `wrangler.toml` — die Datei liegt im öffentlichen Repo.

Der Worker schreibt **nur** `commands.json` im Gist. `order-state.json` gehört
dem Actions-Bot, der darauf laden → ändern → speichern macht; ein Fremdschreiben
dazwischen würde dessen Änderungen verlieren. Ein Test wacht darüber.

## Einrichten

### 1. Discord-App anlegen

1. <https://discord.com/developers/applications> → **New Application**
2. Reiter **Bot** → Bot hinzufügen → **Token** kopieren (nur einmal sichtbar)
3. Reiter **General Information** → **Public Key** kopieren
4. Oben die **Application ID** kopieren

Bot auf den Server einladen — Reiter **OAuth2** → URL Generator:
Scopes `bot` + `applications.commands`, Bot-Permission `Manage Roles`
(braucht später `/setup` für die Rolle). Erzeugte URL öffnen, Server wählen.

### 2. Worker deployen

```bash
cd worker
npm install                       # nur wrangler
npx wrangler login                # öffnet den Browser
npx wrangler secret put DISCORD_PUBLIC_KEY    # Public Key aus Schritt 1.3
npx wrangler deploy
```

Am Ende steht die URL da, etwa
`https://bgnotify-commands.<dein-subdomain>.workers.dev`.

### 3. Endpoint bei Discord eintragen

Im Developer Portal → **General Information** → *Interactions Endpoint URL* →
die Worker-URL eintragen und speichern.

Discord testet die URL sofort: Es schickt ein gültiges PING und zusätzlich
absichtlich einen Request mit **kaputter Signatur**. Gespeichert wird nur, wenn
der Worker den zweiten mit `401` ablehnt. Nimmt Discord die URL an, ist die
Signaturprüfung damit bewiesen — mehr Test braucht es nicht.

### 4. Command registrieren

```bash
DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... DISCORD_GUILD_ID=... node register.js
```

Die Guild-ID (Rechtsklick auf den Server → *Server-ID kopieren*, dafür muss der
Entwicklermodus in den Discord-Einstellungen an sein) sorgt dafür, dass die
Commands sofort da sind. Ohne sie dauert es bis zu einer Stunde.

### 5. Einrichten

Im Server einmalig **`/setup`** aufrufen — nur der Server-Inhaber darf das.
Der Command legt die Rolle `bgnotify` an, gibt sie dir und merkt sie im Gist.

Die Rolle trägt bewusst **keine** Discord-Berechtigungen: Sie ist reines
Kennzeichen für den Worker, kein Recht auf dem Server. Wer sie sonst noch
bekommen soll, kriegt sie über *Servereinstellungen → Mitglieder*.

`/setup` ist gefahrlos wiederholbar — eine schon vorhandene Rolle wird
übernommen statt ein Duplikat anzulegen. Falls der Gist-Eintrag mal verloren
geht, holt ein erneuter Aufruf den Stand zurück.

### 6. Befehlsübersicht in den Channel

Einen Channel für den Bot anlegen und dort **`/panel`** aufrufen — auch das nur
der Server-Inhaber. Die Übersicht bleibt als Nachricht stehen und hält sich
selbst aktuell; darunter tippt man die Commands, deren Antworten ohnehin nur
der Aufrufer sieht.

Wird die Nachricht gelöscht, legt der nächste `/panel`-Aufruf eine neue an.
Braucht der Bot in dem Channel Schreibrechte, sonst meldet `/panel` genau das.

### Fertig, wenn

`/ping` im Server „pong" antwortet, die Antwort nur du siehst — und jemand
**ohne** die Rolle stattdessen eine Absage bekommt.

## Tests

```bash
cd worker && node test.mjs      # Worker
python test_bot.py            # Bot (aus dem Repo-Wurzelverzeichnis)
```

Beide laufen zusaetzlich bei jedem Push (`.github/workflows/tests.yml`) —
ohne Secrets und ohne Netz, die Suiten stellen ihre Raender selbst nach.

Läuft ohne Deploy: echte Ed25519-Schlüssel und echte Signaturen; Discord, die
Gist-API und raw.githubusercontent über ein ersetztes `fetch` nachgestellt.
143 Fälle, darunter „schreibt NUR commands.json" und „Panel wird bearbeitet,
nicht neu gepostet".

## Fehlersuche

| Symptom | Ursache |
|---|---|
| Discord nimmt die URL nicht an | Public Key falsch oder Worker nicht deployt. `npx wrangler tail` zeigt die Requests live. |
| `/ping` taucht nicht auf | Command nicht registriert, oder global statt für die Guild (dauert bis zu 1 h). |
| „Die Anwendung reagiert nicht" | Worker antwortet nicht binnen 3 s — bei `/ping` praktisch nur möglich, wenn er gar nicht läuft. |
| „Auf diesem Server ist noch nichts eingerichtet" | `/setup` wurde noch nie aufgerufen. |
| `/setup` sagt „Missing Permissions" | Dem Bot fehlt `Manage Roles`, **oder** die Rolle `bgnotify` steht in der Serverliste über der Bot-Rolle. Discord lässt einen Bot nur Rollen unterhalb seiner eigenen vergeben — Bot-Rolle nach oben ziehen. |
| `/setup` sagt „Unknown Guild" | Der Bot ist gar nicht auf diesem Server. Einladungslink erneut öffnen. |
| Antwort bleibt ewig bei „denkt nach…" | Die nachgereichte Antwort kam nicht an. `npx wrangler tail` zeigt den Fehler aus `runSetup`. |
| `/panel` sagt „Missing Access" | Dem Bot fehlt in **diesem** Channel das Recht, Nachrichten zu schreiben. Channel-Rechte prüfen oder einen anderen nehmen. |
| Panel zeigt einen alten Stand | Es zieht erst beim nächsten Command nach. Einmal `/ping` genügt — oder direkt `/panel`. |
| Neue Commands fehlen in Discord | `node register.js` vergessen. Das Panel allein reicht nicht, Discord muss sie kennen. |

## Was hier NICHT hingehört

Zugangsdaten zu BG, Webhook-URLs, Discord-IDs. Das Repo ist öffentlich; alles
Geheime lebt in Worker-Secrets, GitHub-Secrets oder im privaten Gist.
