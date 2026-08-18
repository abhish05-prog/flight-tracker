# Flight Price Tracker

Tracks Toronto (YYZ) → Europe flight prices daily, logs history to CSV, emails
you when a deal is found, and publishes a browsable price-history page.
Runs entirely on free tiers: Travelpayouts Data API, GitHub Actions, Gmail SMTP.

## How it works

1. `track_prices.py` runs on a daily GitHub Actions schedule. For each
   destination in `config.yaml`, it queries the Travelpayouts `/v1/prices/cheap`
   endpoint, appends the cheapest fare found to `data/history.csv`, and checks
   two deal triggers:
   - **Floor price**: fare ≤ `floor_price_cad` (default $700).
   - **Statistical deal**: fare ≥ 15% below that route's 90-day rolling
     average, once at least 5 historical data points exist for the route.
2. If any route triggers a deal, an email is sent via Gmail SMTP listing the
   qualifying routes, prices, dates, and the reason(s) they qualified. No
   email is sent on days with no deals.
3. `generate_html.py` rebuilds `docs/history.html` — one card per destination
   with lowest/latest/average price and a full check history table (cheapest
   row highlighted).
4. The GitHub Actions workflow commits the updated CSV and HTML back to the
   repo after each run, so history persists for free without a database.

## One-time setup

### 1. Travelpayouts API token

1. Create a free account at [travelpayouts.com](https://www.travelpayouts.com/)
   and register as an affiliate (approval is usually instant/automatic).
2. Find your API token under **Tools → API access** in the dashboard.

### 2. Gmail App Password

1. Enable 2-Step Verification on the Gmail account you want to send from:
   https://myaccount.google.com/security
2. Create an App Password: https://myaccount.google.com/apppasswords
   (choose "Mail" / "Other" as the app). Copy the 16-character password.

### 3. Push this repo to GitHub

```bash
git init
git add .
git commit -m "Initial commit: flight price tracker"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

### 4. Add repo secrets

In your GitHub repo: **Settings → Secrets and variables → Actions → New
repository secret**. Add:

| Secret name | Value |
|---|---|
| `TRAVELPAYOUTS_TOKEN` | Your Travelpayouts API token |
| `EMAIL_ADDRESS` | The Gmail address to send from |
| `EMAIL_APP_PASSWORD` | The 16-character App Password from step 2 |
| `EMAIL_RECIPIENT` | Where deal alerts should be sent |

### 5. (Optional) Enable GitHub Pages for a bookmarkable history URL

Free GitHub Pages requires a **public** repo (private-repo Pages needs a paid
plan). If you want the public URL, make the repo public first (**Settings →
General → Danger Zone → Change visibility**) — note that everything else in
the repo (code, config, price history) becomes publicly visible too. The
recipient email lives only in the `EMAIL_RECIPIENT` secret, never in a
committed file, so it stays private either way.

Then: **Settings → Pages → Source: Deploy from a branch → Branch: `main`,
folder: `/docs`**. After the next Action run, your history page will be at
`https://<your-username>.github.io/<your-repo>/history.html`.

### 6. Test it

Go to the **Actions** tab → "Track flight prices" → **Run workflow** to
trigger a manual run and confirm everything works before waiting for the
next scheduled run.

## Configuration

Edit `config.yaml` to change:
- `origin` — departure airport code
- `destinations` — list of `{code, name}` destination airports
- `deal_rules.floor_price_cad` — hard price ceiling for an alert
- `deal_rules.statistical_discount_pct` — % below rolling average to trigger
- `deal_rules.min_history_points` — minimum data points before the
  statistical trigger is active
- `deal_rules.rolling_window_days` — lookback window for the average

The alert recipient is set via the `EMAIL_RECIPIENT` secret/env var, not
`config.yaml`, so it isn't exposed if the repo is public.

## Local development

```bash
pip install -r requirements.txt
export TRAVELPAYOUTS_TOKEN=your_token
export EMAIL_ADDRESS=you@gmail.com
export EMAIL_APP_PASSWORD=your_app_password
export EMAIL_RECIPIENT=you@gmail.com
python track_prices.py
python generate_html.py
open docs/history.html
```

## Notes

- Travelpayouts data is crowd-sourced/cached, not live GDS pricing — good for
  trend/alert purposes, not for checkout-level precision. Always verify the
  actual fare on an airline or OTA site before booking.
- Amadeus's free self-service tier was shut down July 17, 2026, so it isn't
  an option here.
