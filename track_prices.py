#!/usr/bin/env python3
"""Fetch cheap-fare data from Travelpayouts, log it to CSV, and email deal alerts."""

import csv
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
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
    reasons = []

    floor = floor_price_cad if floor_price_cad is not None else rules["floor_price_cad"]
    if price <= floor:
        reasons.append(f"at or below floor price of ${floor} CAD")

    past_prices = rolling_average(history, destination, rules["rolling_window_days"])
    if len(past_prices) >= rules["min_history_points"]:
        avg = sum(past_prices) / len(past_prices)
        threshold = avg * (1 - rules["statistical_discount_pct"] / 100)
        if price <= threshold:
            pct_below = (1 - price / avg) * 100
            reasons.append(
                f"{pct_below:.0f}% below the {rules['rolling_window_days']}-day "
                f"average of ${avg:.0f} CAD"
            )

    return reasons


def send_deal_email(config, deals):
    email_cfg = config["email"]
    sender = os.environ["EMAIL_ADDRESS"]
    app_password = os.environ["EMAIL_APP_PASSWORD"]
    recipient = os.environ["EMAIL_RECIPIENT"]

    lines = ["Flight deals found:\n"]
    for deal in deals:
        lines.append(
            f"- {deal['destination_name']} ({deal['destination']}): "
            f"${deal['price']} {deal['currency'].upper()} "
            f"(depart {deal['depart_date']}, return {deal['return_date']}) "
            f"[{deal['airline_name']} {deal['flight_number']}]\n"
            f"  Reason: {', '.join(deal['reasons'])}"
        )
    body = "\n".join(lines)

    msg = MIMEText(body)
    msg["Subject"] = f"{email_cfg['subject_prefix']} {len(deals)} route(s) found"
    msg["From"] = sender
    msg["To"] = recipient

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, app_password)
        server.sendmail(sender, [recipient], msg.as_string())


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
    monthly_cfg = config.get("monthly_prices") or {}
    collect_months = monthly_cfg.get("enabled", True)
    delay = float(monthly_cfg.get("request_delay_seconds", 0.25))

    new_rows = []
    monthly_rows = []
    deals = []

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
            monthly_rows.extend(
                collect_monthly(dest, origin, currency, token, airline_names, now)
            )
            time.sleep(delay)

        if dest.get("alerts", True):
            reasons = evaluate_deal(
                fare["price"], code, history, rules, floor_price_cad=dest.get("floor_price_cad")
            )
            if reasons:
                deals.append({**row, "reasons": reasons})

    if new_rows:
        append_history(new_rows)

    if monthly_rows:
        append_monthly(monthly_rows)
        print(f"Logged {len(monthly_rows)} month/route fares")

    if deals:
        print(f"Found {len(deals)} deal(s), sending email")
        send_deal_email(config, deals)
    else:
        print("No deals found today")


if __name__ == "__main__":
    main()
