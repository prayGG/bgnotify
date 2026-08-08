# bgnotify

## Offene Schritte am PC

Diese Liste ist der Merkzettel für das, was **pray lokal** machen muss — Dinge,
an die der Bot selbst nicht drankommt. Erledigtes hier streichen; steht etwas
noch drin, gilt es als offen und darf beim nächsten passenden Anlass
nachgefragt werden.

### 1 · Worker neu ausrollen

Steht aus seit dem Auswahlmenü für `/product add` und dem Merken des
Command-Channels (PR #15). Solange das nicht läuft, greift im Discord noch der
alte Worker-Stand.

```
cd C:\...\bgnotify\worker
npx wrangler deploy
```

PowerShell 5.1: die beiden Zeilen **einzeln** eingeben — `&&` ist dort ein
Syntaxfehler.

`node register.js` ist **nicht** nötig. Das braucht es nur, wenn sich Name,
Beschreibung oder Optionen eines Commands ändern; die letzten Änderungen
betrafen nur das Verhalten dahinter.

### 2 · `DISCORD_BOT_TOKEN` als GitHub-Actions-Secret

Repo → Settings → Secrets and variables → Actions → New repository secret,
Name `DISCORD_BOT_TOKEN`. Derselbe Token, der schon als Worker-Secret liegt
(`wrangler secret put`) — Actions und Cloudflare teilen sich keine Secrets, er
muss deshalb zweimal hinterlegt werden.

Erst damit gehen Deploy-Karten und Fehler-Reports in den Channel, in dem
zuletzt ein Command lief. Ohne das Secret ändert sich nichts: Dann gilt weiter
`DISCORD_UPDATES_WEBHOOK_URL` — und der zeigt auf einen Channel, den pray nicht
sieht. Genau das war der Grund für den Umbau.

Prüfen lässt es sich ohne Warten: einen beliebigen Command im gewünschten
Channel absetzen (merkt den Channel), dann `/run`. Beim nächsten Deploy oder
Fehler muss die Karte dort auftauchen.

### 3 · `DISCORD_UPDATES_WEBHOOK_URL` — Entscheidung offen

Kann bleiben (greift dann nur noch, wenn der Bot im gemerkten Channel nicht
schreiben darf) oder weg. Kein Handlungsdruck, nur nicht vergessen, dass das
Secret noch existiert und auf einen toten Channel zeigt.

## Testen

```
python test_bot.py      # Entscheidungen des Bots, ohne Netz/Browser/Gist
node worker/test.mjs    # Worker, echte Signaturen gegen worker.fetch()
```

Beide laufen auch bei jedem Push (`.github/workflows/tests.yml`). Vor einem PR
beide grün haben — die Python-Seite kam nachträglich dazu, nachdem ein Fehler
durchgerutscht war, den die Worker-Tests strukturell nicht sehen konnten.
