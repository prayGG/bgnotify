# bgnotify

Privater Discord-Notify-Bot. Läuft als GitHub-Action und überwacht:

- **Shop-Produkte** (WooCommerce) — Restock-/Out-of-stock-Alerts, Preis-History,
  persistentes Status-Dashboard + Stats-Karte
- **PlayStation Store** — Preissenkungs-Alerts für konfigurierte Spiele
- **Forum-Posts** — neue Posts eines bestimmten Autors (XenForo, via Playwright)
- **Bestellstatus** — eigenes Kundenkonto, Status-Updates + Hermes-Tracking
  in einen privaten Channel (Stand im privaten Gist, nie im Repo)
- **Sendungsverfolgung** — sobald bei einer Bestellung ein Tracking-Link
  auftaucht, wird die Sendung automatisch weiterverfolgt: jede Hermes-Station
  als eigene Meldung, bis zur Zustellung. Ohne hinterlegtes Kundenkonto geht
  auch ein Link von Hand (privates Gist, `manual_tracking`)

## Architektur

```
src/
├── main.py          Orchestrator (Einstieg: python -m src.main)
│
│   Scraper — reines Fetch + Parse, kein State:
├── bgpharma.py      WooCommerce-Produkte (simple + variable/AJAX)
├── playstation.py   PS-Store-Preis aus dem eingebetteten Seiten-JSON
├── forum.py         XenForo-Suche via Playwright (Incapsula-Bypass)
├── orders.py        Kundenkonto-Login + Order-Parsing, Gist-State-I/O
│
│   Watcher — State führen, Transitions erkennen:
├── stock_watch.py   Produkt-Checks inkl. Site-wide-Outage-Guard
├── ps_watch.py      PS-Preise (gleiche State-Form wie Produkte)
├── forum_watch.py   neue Posts diffen, intervall-gegated
├── order_watch.py   Bestellungen diffen, Account-Staggering
│
│   Darstellung + Versand:
├── embeds.py        alle Discord-Embed-Builder (Dashboard, Stats, Alerts, …)
├── notify.py        Webhook-Versand (Edit-in-place + Event-Posts, Retries)
├── deploy.py        Deploy-Announcement bei neuem HEAD
│
│   Basis:
├── config.py        config.yml + state.json laden/speichern, Ping-IDs
├── pricing.py       USD→EUR-Kurs, Preis-Parsing/-Formatierung
│
│   Manuelle Tests (Actions → Run workflow):
├── test_ping.py     postet jeden Embed-Typ testweise in seinen Channel
└── order_test.py    Order-Diagnose + Embed-Vorschau (echter Login, read-only)
```

## Dateien

| Datei | Zweck |
|---|---|
| `config.yml` | Produkte, Varianten, Intervalle, Webhook-Env-Namen (keine Secrets!) |
| `state.json` | öffentlicher Bot-State (Preise, History, Message-IDs) — wird vom Workflow nach jedem Run zurückcommittet |
| privates Gist | Order-State (Bestellungen, Cookies, on/off-Schalter `enabled`, verfolgte Sendungen unter `auto_tracking` / `manual_tracking`) |

## Secrets (GitHub Actions)

| Secret | Zweck |
|---|---|
| `DISCORD_WEBHOOK_URL` | Status-Channel (Dashboard + Stats) |
| `DISCORD_STOCK_WEBHOOK_URL` | Restock-/OOS-/PS-Alerts (Fallback: Haupt-Webhook) |
| `DISCORD_UPDATES_WEBHOOK_URL` | Deploy-Announcements |
| `DISCORD_FORUM_WEBHOOK_URL` | Forum-Posts (leer = Feature aus) |
| `DISCORD_ORDER_WEBHOOK_URL` | Bestellstatus (leer = Feature aus) |
| `GIST_TOKEN` / `GIST_ID` | privates Gist für den Order-State |
| `BG_USERNAME[_2]` / `BG_PASSWORD[_2]` | Kundenkonto-Logins (Konto a/b) |
| `DISCORDID` / `WEITERE_ID_HIER` | Discord-User-IDs für @-Pings |

## Lokal ausführen

```bash
pip install -r requirements.txt
python -m playwright install chromium   # nur für Forum-/Order-Features

python -m src.main                       # ein kompletter Bot-Run
python -m src.bgpharma <url> [variante]  # einzelnes Produkt debuggen
python -m src.playstation <url>          # einzelnes PS-Spiel debuggen
python -m src.forum <search_url>         # Forum-Scrape debuggen
```

Webhooks/IDs kommen aus env vars (Namen siehe `config.yml`) — das Repo ist
öffentlich, deshalb stehen hier nirgends echte URLs, IDs oder Zugangsdaten.
