# bgnotify · Discord-Commands

Interactions-Endpoint auf Cloudflare Workers. Nimmt Slash-Commands entgegen und
bearbeitet später das private Gist. **Postet nichts** — Meldungen in die Channels
laufen weiterhin über die Webhooks des Actions-Bots.

Kein dauerlaufender Prozess: Discord schickt jeden Command als HTTPS-POST, der
Worker antwortet. Deshalb dauerhaft kostenlos (Workers-Free: 100.000
Requests/Tag, keine Kreditkarte).

## Stand

Schritt 1 von 7 — Signaturprüfung und `/ping`. Mehr kann der Worker noch nicht,
und das ist Absicht: Discord akzeptiert die Endpoint-URL nur, wenn die
Ed25519-Prüfung korrekt ist. Steht die, ist der Rest geradeaus.

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

### Fertig, wenn

`/ping` im Server „pong" antwortet — und die Antwort nur du siehst.

## Fehlersuche

| Symptom | Ursache |
|---|---|
| Discord nimmt die URL nicht an | Public Key falsch oder Worker nicht deployt. `npx wrangler tail` zeigt die Requests live. |
| `/ping` taucht nicht auf | Command nicht registriert, oder global statt für die Guild (dauert bis zu 1 h). |
| „Die Anwendung reagiert nicht" | Worker antwortet nicht binnen 3 s — bei `/ping` praktisch nur möglich, wenn er gar nicht läuft. |

## Was hier NICHT hingehört

Zugangsdaten zu BG, Webhook-URLs, Discord-IDs. Das Repo ist öffentlich; alles
Geheime lebt in Worker-Secrets, GitHub-Secrets oder im privaten Gist.
