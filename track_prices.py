#!/usr/bin/env python3
"""Fetch cheap-fare data from Travelpayouts, log it to CSV, and email deal alerts."""

import csv
import math
import os
import smtplib
import sys
import time
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText

import requests
import yaml

API_URL = "https://api.travelpayouts.com/v1/prices/cheap"
DIRECT_API_URL = "https://api.travelpayouts.com/v1/prices/direct"
# Returns the cheapest fare for every month Travelpayouts has cached for a
# route, keyed YYYY-MM, in a single request.
MONTHLY_API_URL = "https://api.travelpayouts.com/v1/prices/monthly"
AIRLINES_URL = "https://api.travelpayouts.com/data/en/airlines.json"
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
HISTORY_PATH = os.path.join(os.path.dirname(__file__), "data", "history.csv")
MONTHLY_PATH = os.path.join(os.path.dirname(__file__), "data", "fares_by_month.csv")
# What has already been alerted on, so the same fare is not mailed every day.
ALERT_LOG_PATH = os.path.join(os.path.dirname(__file__), "data", "alerts.csv")
ALERT_LOG_FIELDS = ["sent_at", "destination", "price"]
CSV_FIELDS = [
    "checked_at",
    "origin",
    "destination",
    "destination_name",
    "price",
    "currency",
    "depart_date",
    "return_date",
    "airline",
    "airline_name",
    "flight_number",
]
# One row per route per departure month, so price can be read across the
# calendar rather than only across check dates.
MONTHLY_FIELDS = ["checked_at", "origin", "destination", "destination_name",
                  "depart_month"] + CSV_FIELDS[4:] + ["transfers"]


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def fetch_airline_names():
    try:
        resp = requests.get(AIRLINES_URL, timeout=30)
        resp.raise_for_status()
        return {a["code"]: a["name"] for a in resp.json() if a.get("code")}
    except requests.RequestException as e:
        print(f"Warning: couldn't fetch airline names ({e})", file=sys.stderr)
        return {}


def fetch_monthly_fares(origin, destination, currency, token):
    """Cheapest fare per departure month for one route, in a single request."""
    resp = requests.get(
        MONTHLY_API_URL,
        params={
            "origin": origin,
            "destination": destination,
            "currency": currency,
            "token": token,
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        return {}

    fares = {}
    for month, offer in (payload.get("data") or {}).items():
        if not isinstance(offer, dict) or offer.get("price") is None:
            continue
        fares[month] = {
            "price": offer["price"],
            "depart_date": (offer.get("departure_at") or "")[:10],
            "return_date": (offer.get("return_at") or "")[:10],
            "airline": offer.get("airline", ""),
            "flight_number": offer.get("flight_number", ""),
            "transfers": offer.get("transfers", ""),
        }
    return fares


def fetch_cheapest_fare(origin, destination, currency, token, direct_only=False):
    params = {
        "origin": origin,
        "destination": destination,
        "currency": currency,
        "token": token,
    }
    resp = requests.get(
        DIRECT_API_URL if direct_only else API_URL,
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        return None

    # The API keys results by resolved city code (e.g. LHR -> "LON"), not the
    # requested airport code, so grab the single value instead of indexing by key.
    data = payload.get("data", {})
    offers = next(iter(data.values()), None)
    if not offers:
        return None

    cheapest = min(offers.values(), key=lambda o: o["price"])
    return {
        "price": cheapest["price"],
        "depart_date": (cheapest.get("departure_at") or "")[:10],
        "return_date": (cheapest.get("return_at") or "")[:10],
        "airline": cheapest.get("airline", ""),
        "flight_number": cheapest.get("flight_number", ""),
    }



def read_history():
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH, newline="") as f:
        return list(csv.DictReader(f))


def append_history(rows):
    file_exists = os.path.exists(HISTORY_PATH)
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def append_monthly(rows):
    if not rows:
        return
    file_exists = os.path.exists(MONTHLY_PATH)
    os.makedirs(os.path.dirname(MONTHLY_PATH), exist_ok=True)
    with open(MONTHLY_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MONTHLY_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def collect_monthly(dest, origin, currency, token, airline_names, now):
    """Record every departure month Travelpayouts has cached for one route."""
    code, name = dest["code"], dest["name"]
    # Month prices are supplementary. This endpoint's exact response shape is
    # unverified against a live token, so no failure here may take down the
    # daily check or the deal emails.
    try:
        fares = fetch_monthly_fares(origin, code, currency, token)
    except Exception as e:  # noqa: BLE001 - deliberately broad, see above
        print(f"  {code} months: skipped ({type(e).__name__}: {e})", file=sys.stderr)
        return []

    if not fares:
        print(f"  {code} months: no data")
        return []

    rows = []
    for month in sorted(fares):
        fare = fares[month]
        rows.append({
            "checked_at": now,
            "origin": origin,
            "destination": code,
            "destination_name": name,
            "depart_month": month,
            "price": fare["price"],
            "currency": currency,
            "depart_date": fare["depart_date"],
            "return_date": fare["return_date"],
            "airline": fare["airline"],
            "airline_name": airline_names.get(fare["airline"], fare["airline"]),
            "flight_number": fare["flight_number"],
            "transfers": fare["transfers"],
        })
    span = f"{min(fares)} to {max(fares)}" if fares else "none"
    print(f"  {code} months: {len(rows)} ({span})")
    return rows


def rolling_average(history, destination, window_days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    prices = []
    for row in history:
        if row["destination"] != destination:
            continue
        try:
            checked_at = datetime.fromisoformat(row["checked_at"])
        except ValueError:
            continue
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        if checked_at >= cutoff:
            prices.append(float(row["price"]))
    return prices


def evaluate_deal(price, destination, history, rules, floor_price_cad=None):
    """Reasons this fare is worth mailing about, or an empty list."""
    reasons = []

    floor = floor_price_cad if floor_price_cad is not None else rules["floor_price_cad"]
    if price <= floor:
        reasons.append(f"at or below the ${floor:,.0f} floor")

    past = rolling_average(history, destination, rules["rolling_window_days"])
    if len(past) >= rules["min_history_points"]:
        # Same percentile the history page shows, so the email and the page
        # never disagree about whether today is a good day to book.
        series = sorted(past + [price])
        below = sum(1 for p in series if p < price)
        equal = sum(1 for p in series if p == price)
        rank = (below + 0.5 * equal) / len(series) * 100
        if rank <= rules.get("alert_percentile", 10):
            reasons.append(
                f"cheapest {rank:.0f}% of {len(series)} checks "
                f"(low ${min(series):,.0f}, average ${sum(series) / len(series):,.0f})"
            )

    return reasons


def read_alert_log():
    """Most recent alert per route: destination -> (date, price)."""
    if not os.path.exists(ALERT_LOG_PATH):
        return {}
    latest = {}
    with open(ALERT_LOG_PATH, newline="") as f:
        for row in csv.DictReader(f):
            try:
                sent = date.fromisoformat(row["sent_at"][:10])
                price = float(row["price"])
            except (ValueError, KeyError, TypeError):
                continue
            dest = row.get("destination")
            if dest and (dest not in latest or sent >= latest[dest][0]):
                latest[dest] = (sent, price)
    return latest


def append_alert_log(rows):
    if not rows:
        return
    exists = os.path.exists(ALERT_LOG_PATH)
    os.makedirs(os.path.dirname(ALERT_LOG_PATH), exist_ok=True)
    with open(ALERT_LOG_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ALERT_LOG_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def recently_alerted(destination, price, alert_log, suppress_days, today):
    """True when this route was alerted recently and has not got cheaper."""
    last = alert_log.get(destination)
    if not last or suppress_days <= 0:
        return False
    last_date, last_price = last
    if (today - last_date).days >= suppress_days:
        return False
    return price >= last_price


def _send(config, subject, body):
    sender = os.environ["EMAIL_ADDRESS"]
    app_password = os.environ["EMAIL_APP_PASSWORD"]
    recipient = os.environ["EMAIL_RECIPIENT"]

    msg = MIMEText(body)
    msg["Subject"] = f"{config['email']['subject_prefix']} {subject}"
    msg["From"] = sender
    msg["To"] = recipient

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, app_password)
        server.sendmail(sender, [recipient], msg.as_string())


def send_deal_email(config, deals, page_url=None):
    lines = [
        f"{len(deals)} route(s) worth a look today:",
        "",
    ]
    for deal in deals:
        flight = f"{deal['airline_name']} {deal['flight_number']}".strip()
        lines.append(f"{deal['destination_name']} ({deal['destination']})  "
                     f"${deal['price']:,.0f} {deal['currency'].upper()}")
        lines.append(f"  depart {deal['depart_date']}  return {deal['return_date']}"
                     + (f"  {flight}" if flight else ""))
        lines.append(f"  why: {'; '.join(deal['reasons'])}")
        if deal.get("best_month"):
            month, month_price = deal["best_month"]
            lines.append(f"  cheapest month on record: {month} at ${month_price:,.0f}")
        lines.append("")

    if page_url:
        lines.append(f"Full history: {page_url}")
    lines.append("")
    lines.append("Fares are cached, crowd-sourced data - confirm with the airline "
                 "before booking.")

    _send(config, f"{len(deals)} route(s) found", "\n".join(lines))


def send_health_email(config, problems, page_url=None):
    lines = ["The tracker ran but something looks wrong:", ""]
    lines.extend(f"- {p}" for p in problems)
    lines.append("")
    lines.append("A run that records fewer routes than configured usually means the "
                 "API returned nothing for them, not that the fares vanished.")
    if page_url:
        lines.append("")
        lines.append(f"Full history: {page_url}")
    _send(config, "run health warning", "\n".join(lines))


def check_run_health(config, new_rows, history, today):
    """Problems worth mailing about: missing routes, or a gap since last run."""
    problems = []
    expected = len(config.get("destinations") or [])
    got = len({r["destination"] for r in new_rows})
    # Travelpayouts routinely has nothing cached for a handful of routes, so a
    # normal day returns roughly 21-24 of 27. Demanding all of them would mail
    # a warning daily; alert only on a real shortfall.
    health_cfg = config.get("run_health") or {}
    ratio = float(health_cfg.get("min_routes_fraction", 0.75))
    threshold = health_cfg.get("min_routes") or math.ceil(expected * ratio)
    if expected and got < threshold:
        missing = sorted(
            {d["code"] for d in config["destinations"]}
            - {r["destination"] for r in new_rows}
        )
        problems.append(
            f"only {got} of {expected} routes returned a fare, below the "
            f"{threshold} expected (missing: {', '.join(missing)})"
        )

    days = sorted({r["checked_at"][:10] for r in history})
    if days:
        try:
            last = date.fromisoformat(days[-1])
            gap = (today - last).days
            if gap > 1:
                problems.append(
                    f"no fares recorded for {gap - 1} day(s) before today "
                    f"(last was {days[-1]})"
                )
        except ValueError:
            pass

    return problems


def main():
    config = load_config()
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        sys.exit("TRAVELPAYOUTS_TOKEN environment variable is not set")

    origin = config["origin"]
    currency = config["currency"]
    rules = config["deal_rules"]

    airline_names = fetch_airline_names()
    history = read_history()
    now = datetime.now(timezone.utc).isoformat()

    # One extra request per route records every departure month the API has
    # cached, so the history page can be filtered by any month or season.
    today = datetime.now(timezone.utc).date()
    monthly_cfg = config.get("monthly_prices") or {}
    collect_months = monthly_cfg.get("enabled", True)
    delay = float(monthly_cfg.get("request_delay_seconds", 0.25))

    alert_log = read_alert_log()
    suppress_days = int(rules.get("repeat_suppression_days", 3))
    page_url = (config.get("email") or {}).get("page_url")

    new_rows = []
    monthly_rows = []
    monthly_by_dest = {}
    deals = []
    suppressed = []

    for dest in config["destinations"]:
        code, name = dest["code"], dest["name"]
        try:
            fare = fetch_cheapest_fare(
                origin, code, currency, token, direct_only=dest.get("direct_only", False)
            )
        except requests.RequestException as e:
            print(f"  {code}: request failed ({e})", file=sys.stderr)
            continue

        if not fare:
            print(f"  {code}: no fare data returned")
            continue

        row = {
            "checked_at": now,
            "origin": origin,
            "destination": code,
            "destination_name": name,
            "price": fare["price"],
            "currency": currency,
            "depart_date": fare["depart_date"],
            "return_date": fare["return_date"],
            "airline": fare["airline"],
            "airline_name": airline_names.get(fare["airline"], fare["airline"]),
            "flight_number": fare["flight_number"],
        }
        new_rows.append(row)
        print(f"  {code}: ${fare['price']} {currency.upper()}")

        if collect_months:
            got = collect_monthly(dest, origin, currency, token, airline_names, now)
            monthly_rows.extend(got)
            if got:
                cheapest = min(got, key=lambda r: r["price"])
                monthly_by_dest[code] = (cheapest["depart_month"], cheapest["price"])
            time.sleep(delay)

        if dest.get("alerts", True):
            reasons = evaluate_deal(
                fare["price"], code, history, rules, floor_price_cad=dest.get("floor_price_cad")
            )
            if reasons:
                if recently_alerted(code, fare["price"], alert_log, suppress_days, today):
                    suppressed.append(code)
                else:
                    deals.append({
                        **row,
                        "reasons": reasons,
                        "best_month": monthly_by_dest.get(code),
                    })

    if new_rows:
        append_history(new_rows)

    if monthly_rows:
        append_monthly(monthly_rows)
        print(f"Logged {len(monthly_rows)} month/route fares")

    if suppressed:
        print(f"Suppressed {len(suppressed)} repeat alert(s): {', '.join(suppressed)}")

    if deals:
        print(f"Found {len(deals)} deal(s), sending email")
        send_deal_email(config, deals, page_url)
        append_alert_log([
            {"sent_at": now, "destination": d["destination"], "price": d["price"]}
            for d in deals
        ])
    else:
        print("No new deals today")

    if (config.get("run_health") or {}).get("enabled", True):
        problems = check_run_health(config, new_rows, history, today)
        if problems:
            print(f"Run health problems: {problems}")
            send_health_email(config, problems, page_url)


if __name__ == "__main__":
    main()
