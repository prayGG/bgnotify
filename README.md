# bgnotify

Discord-Benachrichtigung, wenn bei [bgpharmadrugs.to](https://bgpharmadrugs.to/) ausgewählte Produkt-Varianten wieder auf Lager kommen.

Läuft alle 15 Minuten auf **GitHub Actions** (kostenlos für public Repos), kein eigener Server nötig.

## Was es macht

Lädt eine WooCommerce-Produktseite (z. B. `/product/peptides/`), prüft per AJAX (`?wc-ajax=get_variation`) pro gewählter Variante `is_in_stock` und meldet einen Übergang von *out* → *in stock* per Discord-Webhook.

State (was war beim letzten Check in stock) wird in `state.json` im Repo persistiert — die GitHub Action committet die Datei nach jedem Run, damit der nächste Run weiß was sich geändert hat. Notification kommt nur bei tatsächlichem Restock, kein Spam.

## Setup

### 1. Repo auf GitHub anlegen

```sh
cd C:\Users\kim\Downloads\bgnotify
git init
git add .
git commit -m "initial"
# auf github.com einen public Repo "bgnotify" anlegen, dann:
git remote add origin https://github.com/<dein-user>/bgnotify.git
git branch -M main
git push -u origin main
```

> **public Repo**: GitHub Actions ist nur für public Repos unbegrenzt frei. Die Config enthält keine Geheimnisse — der Discord-Webhook landet in GitHub Secrets, nicht im Repo.

### 2. Discord-Webhook erstellen

Discord → Server-Settings (Zahnrad) → **Integrations** → **Webhooks** → *New Webhook* → Channel wählen → **Copy Webhook URL**.

### 3. Webhook als GitHub Secret hinterlegen

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
- Name: `DISCORD_WEBHOOK_URL`
- Value: die kopierte Webhook-URL

### 4. `config.yml` anpassen

```yaml
products:
  - name: "Peptides Sortiment"
    url: "https://bgpharmadrugs.to/product/peptides/"
    watch_variants:
      - "GHK CU 100 mg"
      - "BPC157 10mg"
```

Variant-Namen brauchen nicht 100% exakt zu sein — Matching ist case-insensitiv und tolerant gegen Whitespace.

### 5. Workflow testen

Repo → **Actions** Tab → **bgnotify** → **Run workflow** (manueller Trigger). Logs ansehen. Beim allerersten Lauf werden noch keine Benachrichtigungen gesendet (Baseline-Run), ab dem zweiten Lauf wird gegen den persistierten State verglichen.

## Lokales Testen

```powershell
python -m pip install -r requirements.txt

# einzelnen Variant-Check
python -m src.bgpharma "https://bgpharmadrugs.to/product/peptides/" "GHK CU 100 mg"

# Discord-Webhook testen
$env:DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/…"
python -m src.notify "Test von bgnotify"

# vollständiger Lauf (schreibt state.json)
python -m src.main
```

## Restock-Simulation

Um einen Restock zu simulieren: in `state.json` für eine aktuell in-stock Variante `"in_stock": false` setzen, committen, Workflow manuell triggern → Discord-Nachricht muss kommen.
