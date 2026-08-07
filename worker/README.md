# bgnotify · Discord-Commands

Interactions-Endpoint auf Cloudflare Workers. Nimmt Slash-Commands entgegen und
bearbeitet später das private Gist. **Postet nichts** — Meldungen in die Channels
laufen weiterhin über die Webhooks des Actions-Bots.

Kein dauerlaufender Prozess: Discord schickt jeden Command als HTTPS-POST, der
Worker antwortet. Deshalb dauerhaft kostenlos (Workers-Free: 100.000
Requests/Tag, keine Kreditkarte).

## Stand

Schritt 2 von 7 — Signaturprüfung, Rollenprüfung, `/setup` und `/ping`.

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

### Fertig, wenn

`/ping` im Server „pong" antwortet, die Antwort nur du siehst — und jemand
**ohne** die Rolle stattdessen eine Absage bekommt.

## Tests

```bash
cd worker && node test.mjs
```

Läuft ohne Deploy: echte Ed25519-Schlüssel und echte Signaturen, Discord und
GitHub über ein ersetztes `fetch` nachgestellt. 22 Fälle.

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

## Was hier NICHT hingehört

Zugangsdaten zu BG, Webhook-URLs, Discord-IDs. Das Repo ist öffentlich; alles
Geheime lebt in Worker-Secrets, GitHub-Secrets oder im privaten Gist.
