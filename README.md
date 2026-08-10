# bgnotify

Discord-Bot als GitHub-Action. Überwacht einen WooCommerce-Shop und den eigenen
Bestellstatus, bedient wird er per Slash-Command.

- **Produkte** — Restock- und Out-of-stock-Alerts, Preisverlauf, ein
  Status-Dashboard und eine Stats-Karte, die sich selbst aktualisieren
- **Bestellungen** — Statuswechsel aus dem Kundenkonto in einen privaten Channel
- **Sendungen** — taucht bei einer Bestellung ein Tracking-Link auf, wird die
  Sendung automatisch weiterverfolgt: jede Hermes-Station als eigene Meldung,
  bis zur Zustellung
- **Forum** — neue Beiträge eines bestimmten Autors

Der Bot läuft alle zehn Minuten. Was er dabei erfährt, landet je nach Sorte in
einem anderen Channel; alles Persönliche (Bestellungen, Cookies, Konten) liegt
in einem **privaten Gist**, nie in diesem Repo.

## Commands

Slash-Commands laufen über einen Cloudflare Worker (`worker/`) — Discord
schickt jeden Command als HTTPS-POST dorthin, es braucht also keinen
dauerlaufenden Prozess. Antworten sieht immer nur, wer den Command aufgerufen
hat.

| | |
|---|---|
| `/status` `/account list` `/track list` `/product list` | ansehen |
| `/account add` `/account remove` `/account enable` `/account disable` | Konten |
| `/track add` `/track remove` | Sendungen |
| `/product add` `/product remove` `/product rename` `/product move` | Produkte |
| `/run` | Lauf sofort anstoßen |
| `/setup` `/panel` `/ping` | Einrichtung |

Details und Einrichtung: [`worker/README.md`](worker/README.md).

## Aufbau

```
src/
├── main.py          Orchestrator (Einstieg: python -m src.main)
│
│   Scraper — reines Fetch + Parse, kein State:
├── bgpharma.py      WooCommerce-Produkte (simple + variable/AJAX)
├── forum.py         XenForo-Suche via Playwright
├── orders.py        Kundenkonto-Login + Order-Parsing
├── hermes.py        Sendungsverlauf
│
│   Watcher — State führen, Änderungen erkennen:
├── stock_watch.py   Produkt-Checks inkl. Outage-Guard
├── forum_watch.py   neue Beiträge
├── order_watch.py   Bestellungen, ein Konto pro Lauf
├── hermes_watch.py  Sendungen bis zur Zustellung
├── product_watch.py per Command aufgenommene Produkte
│
│   Darstellung + Versand:
├── embeds.py        alle Discord-Embeds
├── notify.py        Webhook-Versand (Edit-in-place, Retries)
├── deploy.py        Deploy-Karte bei neuem Stand
├── health.py        Fehler-Report am Lauf-Ende
│
│   Basis:
├── commands.py      liest, was per Slash-Command gesetzt wurde
├── config.py        config.yml + state.json
└── pricing.py       USD→EUR, Preis-Formatierung
```

| Wo | Was |
|---|---|
| `config.yml` | Produkte, Intervalle, Namen der Env-Variablen — keine Werte |
| `state.json` | öffentlicher Stand: Preise, Verlauf, Message-IDs |
| privates Gist | Bestellungen, Konten, Sendungen, Command-Wünsche |

## Secrets

Alles Geheime kommt aus GitHub-Secrets; in diesem Repo stehen nur deren Namen.

| Secret | Zweck |
|---|---|
| `DISCORD_WEBHOOK_URL` | Dashboard + Stats |
| `DISCORD_STOCK_WEBHOOK_URL` | Restock-/OOS-Alerts |
| `DISCORD_ORDER_WEBHOOK_URL` | Bestellungen + Sendungen |
| `DISCORD_FORUM_WEBHOOK_URL` | Forum-Beiträge |
| `DISCORD_UPDATES_WEBHOOK_URL` | Deploy-Karten + Fehler-Reports |
| `GIST_TOKEN` / `GIST_ID` | privates Gist |
| `BG_USERNAME` / `BG_PASSWORD` | Kundenkonto |
| `PING_USER_IDS` | wer bei Alerts gepingt wird |

Weitere Konten kommen nicht hierher, sondern per `/account add` — Zugangsdaten
werden dabei verschlüsselt als Secret abgelegt, der Anzeigename ins private
Gist.

## Lokal

```bash
pip install -r requirements.txt
python -m playwright install chromium

python -m src.main                       # kompletter Lauf
python -m src.bgpharma <url> [variante]  # ein Produkt
python -m src.forum <url>                # Forum-Scrape

python test_bot.py                       # Tests Bot
node worker/test.mjs                     # Tests Worker
```
