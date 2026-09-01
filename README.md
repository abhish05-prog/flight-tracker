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
2. For each route it makes one further request to `/v1/prices/monthly`, which
   returns the cheapest fare for **every departure month** Travelpayouts has
   cached for that route in a single response, and appends them to
   `data/fares_by_month.csv`. This is a separate series from `history.csv`:
   the daily check asks "cheapest fare on any date", which makes its
   `depart_date` a by-product rather than a dimension you can filter on, so
   month questions need their own sampling.
3. Two kinds of email go out via Gmail SMTP:
   - **Deal alerts** when a route is at or below `floor_price_cad`, or lands in
     the cheapest `alert_percentile`% of its own recorded history — the same
     percentile the history page shows, so the email and the page never
     disagree. A route that already alerted within
     `repeat_suppression_days` is skipped unless it got cheaper still, and
     `data/alerts.csv` records what was sent so that survives between runs.
   - **Run health warnings** when fewer routes came back than are configured,
     or a day was missed entirely, so silent failures aren't invisible.

   No email is sent on days with nothing to report.
4. `generate_html.py` rebuilds `docs/history.html` — one card per destination
   showing a **book-or-wait signal**, an inline sparkline of the price trend,
   headline stats, and a collapsible check history.

   The signal scores today's fare against *that route's own* recorded history
   using a percentile rank — the share of past checks that came in cheaper. A
   rank in the bottom 20% reads "Good time to book", the top 40% reads "Above
   its usual range", and the middle is "Middle of its range". Routes with
   fewer than 14 checks say so on the card, since the percentile is thin until
   history builds up. Tuning lives in the constants at the top of
   `generate_html.py` (`GOOD_MAX_PCT`, `FAIR_MAX_PCT`, `PROVISIONAL_BELOW`).

   The sparkline is inline SVG — no external assets, so the page stays a single
   self-contained file. It marks the lowest point (hollow dot), the latest
   point (filled dot), and the route's average (dashed line).

   Cards are ordered **best signal first** (lowest percentile rank), so the
   routes most worth booking today sit at the top. A sort bar switches between
   *Best signal*, *Lowest price*, *Biggest drop* (largest fall since the
   previous check), and *A–Z*; the choice is remembered in `localStorage`.
   Sorting is a few lines of inline JavaScript that just reorder existing
   cards — with JS disabled the page still renders fully, in best-signal
   order. Routes without enough history to score sort last under every key.

   Where month data exists, each card also gets a **by travel month** bar
   chart — cheapest fare per departure month, cheapest month highlighted — and
   *Travel month* and *Season* filters appear above the cards. Picking a month
   or a season highlights the matching bars on every chart, dims routes with no
   fare in that range, and reorders the cards by their cheapest fare within it,
   so "who is cheapest in winter" is one click. The whole month section is
   omitted when `data/fares_by_month.csv` doesn't exist, so the page is
   unchanged until the first run that collects it.
5. The GitHub Actions workflow commits the updated CSV and HTML back to the
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

### Month prices

```yaml
monthly_prices:
  enabled: true
  request_delay_seconds: 0.25
```

`/v1/prices/monthly` returns every month it has for a route in one request, so
there are no months to choose at collection time — everything available is
recorded, and the history page filters it. This costs **one extra API call per
route per day** (about 54 calls a day in total, including the daily
cheapest-any-date checks).

Two caveats:

- The monthly endpoint has no nonstop-only mode, so for a `direct_only` route
  such as Delhi the month bars include connecting flights and are not directly
  comparable to that route's daily nonstop check. The card says so.
- Month history **cannot be backfilled**: the series starts from the first run
  after you enable it.

### Everything else

Edit `config.yaml` to change:
- `origin` — departure airport code
- `destinations` — list of `{code, name}` destination airports
- `deal_rules.floor_price_cad` — always alert at or below this price. **This
  is the biggest driver of how often you get mail**: routes that normally sit
  just under it (Paris, Lisbon) will alert on ordinary days. Replaying the real
  history, dropping it from $600 to $550 takes alerts from 7 days in 13 to 2.
- `deal_rules.alert_percentile` — alert when the fare is in the cheapest N% of
  that route's history
- `deal_rules.min_history_points` — minimum data points before the percentile
  trigger is active
- `deal_rules.rolling_window_days` — lookback window for the percentile
- `deal_rules.repeat_suppression_days` — don't re-alert a route within this
  many days unless it got cheaper still
- `run_health.enabled` — send warning emails on short or missed runs
- `email.page_url` — linked at the bottom of each email

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
