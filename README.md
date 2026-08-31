# Product Updates

`Product Updates` is a local price monitor. It stores every observed price in SQLite and alerts only when a price, availability, or listing changes. The starter configuration monitors the base iPhone 17 for PIN `560037` and excludes Pro/Air/used/accessory listings.

## Quick start (Windows PowerShell)

```powershell
cd Product_Updates
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[browser,dev]"
playwright install chromium
Copy-Item .env.example .env
product-updates check
product-updates run
```

Set Telegram, SMTP, or webhook settings in `.env` for alerts. `check` performs one scan; `run` checks every 60 minutes by default. The local database is `data/prices.sqlite3`.

## Retailer coverage

The app scans Amazon India, Flipkart, Blinkit, BigBasket, Croma, Reliance Digital, and Vijay Sales through their search pages. Retailer layouts, location requirements, and anti-bot controls can prevent an automated read; the app reports those sources rather than inventing a price. For the highest reliability, add direct product page links under `listing_urls` in `config.yaml` once you find them.

## Start automatically with Windows

After setting a notification destination in `.env`, run:

```powershell
.\scripts\install-scheduled-task.ps1
```

This starts the monitor at sign-in and restarts it if it exits. The first successful scan saves a baseline silently; notifications begin with a later detected change.
