#!/usr/bin/env python3
"""Build docs/history.html from data/history.csv."""

import csv
import html
import json
import os
from bisect import bisect_left
from collections import defaultdict
from datetime import date, datetime

import yaml

HISTORY_PATH = os.path.join(os.path.dirname(__file__), "data", "history.csv")
MONTHLY_PATH = os.path.join(os.path.dirname(__file__), "data", "fares_by_month.csv")
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "docs", "history.html")

# Every fare is scored against its own route's history: the percentile rank is
# the share of recorded checks that came in cheaper, so a low rank is a cheap
# day. Bands below split that into book / watch / wait.
GOOD_MAX_PCT = 20
FAIR_MAX_PCT = 60

# Under this many checks the percentile is thin enough that we say so out loud.
PROVISIONAL_BELOW = 14

BAND_VERDICT = {
    "good": "Good time to book",
    "fair": "Middle of its range",
    "high": "Above its usual range",
    "new": "Tracking started",
}


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_rows():
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH, newline="") as f:
        return list(csv.DictReader(f))


def load_monthly():
    """Latest recorded fare per route per departure month."""
    if not os.path.exists(MONTHLY_PATH):
        return {}
    with open(MONTHLY_PATH, newline="") as f:
        rows = list(csv.DictReader(f))

    freshest = {}
    for r in rows:
        key = (r["destination"], r["depart_month"])
        if key not in freshest or r["checked_at"] > freshest[key]["checked_at"]:
            freshest[key] = r

    out = defaultdict(dict)
    for (dest, month), r in freshest.items():
        try:
            out[dest][month] = float(r["price"])
        except (TypeError, ValueError):
            continue
    return {dest: dict(sorted(months.items())) for dest, months in out.items()}


def month_label(month):
    try:
        return datetime.strptime(month, "%Y-%m").strftime("%b")
    except ValueError:
        return month


def month_range_label(months):
    if not months:
        return ""
    first, last = months[0], months[-1]
    try:
        a = datetime.strptime(first, "%Y-%m").strftime("%b %Y")
        b = datetime.strptime(last, "%Y-%m").strftime("%b %Y")
    except ValueError:
        return ""
    return a if a == b else f"{a} - {b}"


def band_for(pct_rank):
    if pct_rank <= GOOD_MAX_PCT:
        return "good"
    if pct_rank <= FAIR_MAX_PCT:
        return "fair"
    return "high"


def percentile_rank(sorted_prices, price):
    """Share of observations below `price`, ties counted at their midpoint."""
    n = len(sorted_prices)
    if n == 0:
        return 50.0
    below = bisect_left(sorted_prices, price)
    equal = sum(1 for p in sorted_prices[below:] if p == price)
    return (below + 0.5 * equal) / n * 100


def parse_day(value):
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def pretty_day(value):
    day = parse_day(value)
    return day.strftime("%b %-d") if day else value[:10]


def pretty_fare_day(value, reference):
    day = parse_day(value)
    if not day:
        return value[:10] or "-"
    ref = parse_day(reference)
    if ref and day.year != ref.year:
        return day.strftime("%b %-d, %Y")
    return day.strftime("%b %-d")


def build_signal(rows_sorted):
    """Where does today's fare sit inside this route's own price history?"""
    prices = [float(r["price"]) for r in rows_sorted]
    days = [r["checked_at"][:10] for r in rows_sorted]
    latest, lowest, count = prices[-1], min(prices), len(prices)
    avg = sum(prices) / count

    stats = {
        "latest": latest,
        "lowest": lowest,
        "avg": avg,
        "count": count,
        "highest": max(prices),
        "prices": prices,
        "change": prices[-1] - prices[-2] if count > 1 else None,
        "provisional": count < PROVISIONAL_BELOW,
    }

    if count < 2:
        stats.update(band="new", pct_rank=None, headline="First check recorded — the "
                     "signal appears once there's a history to compare against.")
        return stats

    pct_rank = percentile_rank(sorted(prices), latest)
    band = band_for(pct_rank)

    if lowest == max(prices):
        stats.update(band="fair", pct_rank=pct_rank,
                     headline=f"Unchanged at ${latest:,.0f} across all {count} checks")
        return stats

    parts = []
    if latest <= lowest:
        parts.append(f"Lowest fare recorded in {count} checks")
    else:
        parts.append(f"Cheaper than {100 - pct_rank:.0f}% of the {count} checks so far")

    gap = abs(latest - avg)
    if gap >= 5:
        direction = "below" if latest < avg else "above"
        parts.append(f"${gap:.0f} {direction} its ${avg:.0f} average")

    # How long since it was last this cheap — only worth saying if the run of
    # pricier days is long enough to mean something.
    if latest > lowest:
        for i in range(count - 2, -1, -1):
            if prices[i] <= latest:
                first, last = parse_day(days[i]), parse_day(days[-1])
                if first and last and (last - first).days >= 3:
                    parts.append(f"cheapest in {(last - first).days} days")
                break

    stats.update(band=band, pct_rank=pct_rank, headline=" · ".join(parts))
    return stats


def month_chart(months, code):
    """Bar chart of cheapest fare per departure month."""
    if not months:
        return ""
    keys = list(months.keys())
    values = [months[k] for k in keys]
    n = len(keys)
    lo, hi = min(values), max(values)
    span = hi - lo
    # With every month at the same price there is no cheapest month to mark.
    cheapest = keys[values.index(lo)] if span > 0 else None

    width, top, plot, labels = 720.0, 20.0, 78.0, 20.0
    height = top + plot + labels
    slot = width / n
    bar_w = min(88.0, slot * 0.55)

    parts = [
        f'<svg class="months-chart" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'width="{width:.0f}" height="{height:.0f}" role="img" '
        f'aria-label="Cheapest fare by departure month for {html.escape(code)}">'
    ]
    for i, key in enumerate(keys):
        price = months[key]
        # Floor the bar so a small spread still reads as a bar, not a sliver.
        frac = 1.0 if span == 0 else 0.18 + 0.82 * (price - lo) / span
        bar_h = plot * frac
        x = slot * i + (slot - bar_w) / 2
        y = top + (plot - bar_h)
        is_low = key == cheapest
        cls = "bar bar-low" if is_low else "bar"
        parts.append(
            f'<g class="month-group" data-month="{key}">'
            f'<rect class="{cls}" x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{bar_h:.1f}" rx="3"/>'
            f'<text class="bar-price{" bar-price-low" if is_low else ""}" '
            f'x="{x + bar_w / 2:.1f}" y="{y - 6:.1f}" text-anchor="middle">'
            f'${price:,.0f}</text>'
            f'<text class="bar-label{" bar-label-low" if is_low else ""}" '
            f'x="{x + bar_w / 2:.1f}" y="{height - 6:.1f}" text-anchor="middle">'
            f'{month_label(key)}</text>'
            f'</g>'
        )
    parts.append("</svg>")
    return "".join(parts)


def sparkline(prices, band, uid, name):
    """Inline SVG trend line — no scripts, no external assets."""
    width, height, pad_x, pad_y = 720.0, 104.0, 7.0, 14.0
    n = len(prices)
    lo, hi = min(prices), max(prices)
    span = hi - lo
    label = (
        f"{name} price trend over {n} checks, "
        f"${lo:.0f} to ${hi:.0f}, latest ${prices[-1]:.0f}"
    )

    def px(i):
        return pad_x + (width - 2 * pad_x) * (i / (n - 1) if n > 1 else 0.5)

    def py(price):
        if span == 0:
            return height / 2
        return pad_y + (height - 2 * pad_y) * (1 - (price - lo) / span)

    points = [(px(i), py(p)) for i, p in enumerate(prices)]
    open_svg = (
        f'<svg class="spark spark-{band}" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'width="{width:.0f}" height="{height:.0f}" role="img" '
        f'aria-label="{html.escape(label)}">'
    )

    if n == 1:
        return ""

    line = " ".join(
        f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(points)
    )
    area = f"{line} L{points[-1][0]:.1f},{height:.1f} L{points[0][0]:.1f},{height:.1f} Z"
    avg_y = py(sum(prices) / n)
    min_i = prices.index(lo)

    return (
        f"{open_svg}"
        f'<defs><linearGradient id="fill-{uid}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="currentColor" stop-opacity="0.20"/>'
        f'<stop offset="1" stop-color="currentColor" stop-opacity="0"/>'
        f"</linearGradient></defs>"
        f'<line class="spark-avg" x1="{pad_x:.1f}" y1="{avg_y:.1f}" '
        f'x2="{width - pad_x:.1f}" y2="{avg_y:.1f}"/>'
        f'<path d="{area}" fill="url(#fill-{uid})"/>'
        f'<path class="spark-line" d="{line}"/>'
        f'<circle class="spark-min" cx="{points[min_i][0]:.1f}" '
        f'cy="{points[min_i][1]:.1f}" r="3.4"/>'
        f'<circle class="spark-dot" cx="{points[-1][0]:.1f}" '
        f'cy="{points[-1][1]:.1f}" r="4.6"/>'
        f"</svg>"
    )



STYLES = """
  :root {
    --paper: #faf9f7;
    --surface: #ffffff;
    --ink: #17191c;
    --ink-soft: #5f656d;
    --ink-faint: #8d929a;
    --hairline: #e7e3dc;
    --hairline-soft: #f1eee9;
    --accent: #8f6425;
    --accent-bg: #fdfaf3;
    --good: #1c7a4b;
    --good-bg: #e5f2ea;
    --fair: #8f6425;
    --fair-bg: #f9f0dd;
    --high: #a63a30;
    --high-bg: #fae7e4;
    --shadow: 0 1px 2px rgba(23, 25, 28, 0.04), 0 10px 28px -20px rgba(23, 25, 28, 0.35);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --paper: #131518;
      --surface: #1a1d21;
      --ink: #e9e7e3;
      --ink-soft: #a2a8b0;
      --ink-faint: #767c85;
      --hairline: #2b2f35;
      --hairline-soft: #23262b;
      --accent: #d2a05c;
      --accent-bg: #201c15;
      --good: #63c894;
      --good-bg: #16301f;
      --fair: #d2a05c;
      --fair-bg: #2c2517;
      --high: #e08379;
      --high-bg: #33201d;
      --shadow: 0 1px 2px rgba(0, 0, 0, 0.3), 0 10px 28px -20px rgba(0, 0, 0, 0.8);
    }
  }
"""
STYLES += """
  * { box-sizing: border-box; }
  body {
    font-family: ui-sans-serif, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    max-width: 60rem; margin: 0 auto; padding: 3.5rem 1.5rem 5rem;
    color: var(--ink); background: var(--paper); line-height: 1.55;
    -webkit-font-smoothing: antialiased;
  }
  .masthead { border-bottom: 1px solid var(--hairline); padding-bottom: 1.5rem; margin-bottom: 2.75rem; }
  .masthead h1 {
    font-family: ui-serif, Georgia, "Times New Roman", serif;
    font-size: 2rem; font-weight: 500; letter-spacing: -0.015em; margin: 0 0 0.4rem;
  }
  .masthead p { margin: 0; color: var(--ink-soft); font-size: 0.9rem; }
  .masthead .meta { color: var(--ink-faint); font-size: 0.8rem; margin-top: 0.35rem; }
  h2.section {
    font-size: 0.72rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.11em; color: var(--ink-faint);
    margin: 3rem 0 1.1rem; padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--hairline-soft);
  }
  h2.section:first-of-type { margin-top: 0; }
  .card {
    background: var(--surface); border: 1px solid var(--hairline);
    border-radius: 12px; padding: 1.4rem 1.5rem; margin-bottom: 1rem;
    box-shadow: var(--shadow);
  }
  .card.priority { border-color: var(--accent); background: var(--accent-bg); }
  .card-head {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: 1rem; flex-wrap: wrap; margin-bottom: 0.5rem;
  }
  .card h3 {
    font-family: ui-serif, Georgia, "Times New Roman", serif;
    font-size: 1.3rem; font-weight: 500; margin: 0; letter-spacing: -0.01em;
  }
  .card h3 .code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.7rem; color: var(--ink-faint); letter-spacing: 0.06em;
    margin-left: 0.5rem; vertical-align: middle;
  }
  .badge {
    display: inline-block; font-size: 0.62rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.09em; color: var(--accent);
    border: 1px solid var(--accent); border-radius: 999px;
    padding: 0.12rem 0.5rem; margin-left: 0.6rem; vertical-align: middle;
  }
"""
STYLES += """
  .verdict {
    font-size: 0.72rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.08em; padding: 0.25rem 0.65rem; border-radius: 999px;
    white-space: nowrap;
  }
  .verdict-good { color: var(--good); background: var(--good-bg); }
  .verdict-fair { color: var(--fair); background: var(--fair-bg); }
  .verdict-high { color: var(--high); background: var(--high-bg); }
  .verdict-new  { color: var(--ink-soft); background: var(--hairline-soft); }
  .signal { font-size: 0.92rem; color: var(--ink-soft); margin: 0 0 1rem; }
  .signal .lead { color: var(--ink); font-weight: 500; }
  .provisional { display: block; font-size: 0.78rem; color: var(--ink-faint); margin-top: 0.25rem; }
  .spark { display: block; width: 100%; height: auto; margin: 0 0 1.1rem; }
  .spark-good { color: var(--good); }
  .spark-fair { color: var(--fair); }
  .spark-high { color: var(--high); }
  .spark-new  { color: var(--ink-faint); }
  .spark-line {
    fill: none; stroke: currentColor; stroke-width: 1.75;
    stroke-linejoin: round; stroke-linecap: round; vector-effect: non-scaling-stroke;
  }
  .spark-avg {
    stroke: var(--ink-faint); stroke-width: 1; stroke-dasharray: 2 4;
    opacity: 0.5; vector-effect: non-scaling-stroke;
  }
  .spark-dot { fill: currentColor; stroke: var(--surface); stroke-width: 1.5; }
  .spark-min { fill: var(--surface); stroke: currentColor; stroke-width: 1.5; }
  .card.priority .spark-dot { stroke: var(--accent-bg); }
  .card.priority .spark-min { fill: var(--accent-bg); }
  .stats {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(6.5rem, 1fr));
    gap: 0.25rem 1.25rem; margin: 0 0 0.5rem;
    border-top: 1px solid var(--hairline-soft); padding-top: 0.9rem;
  }
  .stats div { display: flex; flex-direction: column-reverse; }
  .stats dt {
    font-size: 0.66rem; text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--ink-faint); margin: 0;
  }
  .stats dd {
    margin: 0; font-size: 1.05rem; font-weight: 500;
    font-variant-numeric: tabular-nums; letter-spacing: -0.01em;
  }
  .stats dd.down { color: var(--good); }
  .stats dd.up { color: var(--high); }
"""
STYLES += """
  details { margin-top: 0.75rem; }
  details summary {
    cursor: pointer; color: var(--ink-faint); font-size: 0.78rem;
    text-transform: uppercase; letter-spacing: 0.07em; list-style: none;
    padding: 0.35rem 0; user-select: none;
  }
  details summary::-webkit-details-marker { display: none; }
  details summary::before { content: "+ "; font-weight: 600; }
  details[open] summary::before { content: "- "; }
  details summary:hover { color: var(--ink); }
  .table-wrap { overflow-x: auto; margin-top: 0.5rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.83rem; }
  th {
    text-align: left; font-size: 0.66rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.08em; color: var(--ink-faint);
    padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--hairline);
    white-space: nowrap;
  }
  td {
    padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--hairline-soft);
    color: var(--ink-soft); white-space: nowrap;
  }
  th.num, td.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr:last-child td { border-bottom: none; }
  tr.cheapest td { color: var(--ink); font-weight: 600; }
  tr.cheapest td:first-child::before { content: "* "; color: var(--good); }
  td.price-good { color: var(--good); font-weight: 600; }
  td.price-fair { color: var(--fair); }
  td.price-high { color: var(--high); }
  .no-data { color: var(--ink-faint); font-style: italic; margin: 0.25rem 0 0; }
  .legend { font-size: 0.76rem; color: var(--ink-faint); margin: 0.9rem 0 0; }
  .legend b { font-weight: 600; }
  .legend .k-good { color: var(--good); }
  .legend .k-fair { color: var(--fair); }
  .legend .k-high { color: var(--high); }
  footer {
    margin-top: 3.5rem; padding-top: 1.25rem; border-top: 1px solid var(--hairline);
    font-size: 0.78rem; color: var(--ink-faint);
  }
  footer p { margin: 0 0 0.4rem; }
  @media (max-width: 34rem) {
    body { padding: 2.25rem 1rem 3rem; }
    .masthead h1 { font-size: 1.6rem; }
    .card { padding: 1.15rem 1.1rem; }
  }
"""


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Flight Price History - YYZ</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
{styles}
</style>
</head>
<body>
<header class="masthead">
  <h1>Flight Price History</h1>
  <p>Cheapest return fares from Toronto (YYZ), checked daily.</p>
  <p class="meta">{meta}</p>
</header>
{sortbar}
{priority_section}
{cards}
<footer>
  <p>Each fare is scored against its own route's recorded history, not against
  the other routes. Percentiles need history to mean much: treat a route with
  only a couple of weeks behind it as a rough read.</p>
  <p>Month bars are scaled to each route's own cheapest-to-priciest range
  rather than from zero, so the shape of a season is easy to read but the
  height gap between two bars overstates the price gap. The figure above each
  bar is the actual fare.</p>
  <p>Fares come from Travelpayouts' cached, crowd-sourced data &mdash; good for
  spotting trends, not for checkout-level precision. Always confirm the real
  price with the airline before booking.</p>
</footer>
<script>
{script}
</script>
</body>
</html>
"""

CARD_TEMPLATE = """<article class="card{priority_class}" data-rank="{sort_rank}" data-price="{sort_price}" data-change="{sort_change}" data-name="{sort_name}" data-months='{sort_months}'>
  <div class="card-head">
    <h3>{name}<span class="code">{code}</span>{badge}</h3>
    <span class="verdict verdict-{band}">{verdict}</span>
  </div>
  <p class="signal"><span class="lead">{headline}</span>{provisional}</p>
  {spark}
  {months_block}
  <dl class="stats">
    <div><dd>${latest:,.0f}</dd><dt>Latest</dt></div>
    <div><dd class="{change_class}">{change}</dd><dt>Change</dt></div>
    <div><dd>${lowest:,.0f}</dd><dt>Lowest seen</dt></div>
    <div><dd>${avg:,.0f}</dd><dt>Average</dt></div>
    <div><dd>{count}</dd><dt>Checks</dt></div>
  </dl>
  {legend}
  <details>
    <summary>Full history</summary>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Checked</th><th class="num">Price</th><th>Depart</th><th>Return</th><th>Flight</th></tr></thead>
        <tbody>
        {rows}
        </tbody>
      </table>
    </div>
  </details>
</article>
"""

NO_DATA_CARD_TEMPLATE = """<article class="card priority" data-rank="999" data-price="999999" data-change="999999" data-name="{sort_name}" data-months='{{}}'>
  <div class="card-head">
    <h3>{name}<span class="code">{code}</span><span class="badge">Priority</span></h3>
    <span class="verdict verdict-new">No data yet</span>
  </div>
  <p class="no-data">No fare cached for this route yet &mdash; it appears here once
  Travelpayouts has a price to report.{extra}</p>
</article>
"""

MONTHS_TEMPLATE = """<div class="months">
    <div class="months-head">
      <span class="months-title">By travel month</span>
      <span class="months-range">{range_label}</span>
      <span class="months-summary">{summary}</span>
    </div>
    {chart}
  </div>
"""

LEGEND_HTML = (
    '<p class="legend">Prices in the history below are shaded by percentile: '
    '<b class="k-good">cheap</b> (bottom 20% of days seen), '
    '<b class="k-fair">middling</b>, '
    '<b class="k-high">pricey</b> (top 40%). No deal alerts on this route.</p>'
)


def build_card(code, name, rows, is_priority, color_coded, months=None,
               direct_only=False):
    rows_sorted = sorted(rows, key=lambda r: r["checked_at"])
    signal = build_signal(rows_sorted)
    lowest = signal["lowest"]
    sorted_prices = sorted(signal["prices"])
    cheapest_at = max(
        r["checked_at"] for r in rows_sorted if float(r["price"]) == lowest
    )

    table_rows = []
    for r in reversed(rows_sorted):
        price = float(r["price"])
        if color_coded and signal["count"] > 1:
            price_cls = f' class="num price-{band_for(percentile_rank(sorted_prices, price))}"'
        else:
            price_cls = ' class="num"'
        row_cls = ' class="cheapest"' if r["checked_at"] == cheapest_at else ""
        airline = (r.get("airline_name") or r.get("airline") or "").strip()
        flight = f"{airline} {(r.get('flight_number') or '').strip()}".strip() or "-"
        table_rows.append(
            f"<tr{row_cls}><td>{html.escape(pretty_day(r['checked_at']))}</td>"
            f"<td{price_cls}>${price:,.0f}</td>"
            f"<td>{html.escape(pretty_fare_day(r['depart_date'], r['checked_at']))}</td>"
            f"<td>{html.escape(pretty_fare_day(r['return_date'], r['checked_at']))}</td>"
            f"<td>{html.escape(flight)}</td></tr>"
        )

    change = signal["change"]
    if change is None:
        change_text, change_class = "-", ""
    elif change == 0:
        change_text, change_class = "no change", ""
    else:
        arrow = "down" if change < 0 else "up"
        change_text = f"{'-' if change < 0 else '+'}${abs(change):,.0f}"
        change_class = arrow

    provisional = ""
    if signal["provisional"] and signal["count"] > 1:
        provisional = (
            f'<span class="provisional">Based on {signal["count"]} checks &mdash; '
            f"the read firms up as history builds.</span>"
        )

    # A route with no comparison history sorts last under every key rather
    # than pretending to be a good or bad buy.
    has_signal = signal["pct_rank"] is not None
    sort_meta = {
        "rank": signal["pct_rank"] if has_signal else 999.0,
        "price": signal["latest"],
        "change": change if change is not None else 999999.0,
        "name": name,
    }

    months = months or {}
    if months:
        keys = list(months.keys())
        low_key = min(keys, key=lambda k: months[k])
        spread = max(months.values()) - min(months.values())
        if spread > 0:
            summary = html.escape(
                f"cheapest {month_label(low_key)} at ${months[low_key]:,.0f}, "
                f"${spread:,.0f} spread"
            )
        else:
            summary = html.escape(
                f"level at ${months[low_key]:,.0f} across every month"
            )
        # The monthly endpoint has no nonstop-only mode, so a direct_only
        # route's month bars are not comparable to its daily nonstop check.
        if direct_only:
            summary += " &middot; includes connections"
        months_block = MONTHS_TEMPLATE.format(
            range_label=html.escape(month_range_label(keys)),
            summary=summary,
            chart=month_chart(months, code),
        )
    else:
        months_block = ""

    return sort_meta, CARD_TEMPLATE.format(
        months_block=months_block,
        sort_months=html.escape(json.dumps({k: round(v) for k, v in months.items()})),
        name=html.escape(name),
        sort_rank=f"{sort_meta['rank']:.2f}",
        sort_price=f"{sort_meta['price']:.0f}",
        sort_change=f"{sort_meta['change']:.0f}",
        sort_name=html.escape(name),
        code=html.escape(code),
        band=signal["band"],
        verdict=BAND_VERDICT[signal["band"]],
        headline=html.escape(signal["headline"]),
        provisional=provisional,
        spark=sparkline(signal["prices"], signal["band"], html.escape(code), html.escape(name)),
        latest=signal["latest"],
        lowest=lowest,
        avg=signal["avg"],
        count=signal["count"],
        change=change_text,
        change_class=change_class,
        rows="\n        ".join(table_rows),
        priority_class=" priority" if is_priority else "",
        badge='<span class="badge">Priority</span>' if is_priority else "",
        legend=LEGEND_HTML if color_coded and signal["count"] > 1 else "",
    )


def build_meta(rows, route_count):
    updated = max((r["checked_at"] for r in rows), default=None)
    if not updated:
        return "No checks recorded yet."
    try:
        stamp = datetime.fromisoformat(updated).strftime("%B %-d, %Y at %H:%M UTC")
    except ValueError:
        stamp = updated
    days = len({r["checked_at"][:10] for r in rows})
    first = min(r["checked_at"] for r in rows)
    return (
        f"Last updated {stamp} &middot; {route_count} routes &middot; "
        f"{len(rows):,} checks over {days} days since {pretty_day(first)}"
    )


def main():
    config = load_config()
    destinations = {d["code"]: d for d in config.get("destinations", [])}
    priority_codes = [code for code, d in destinations.items() if d.get("priority")]

    rows = load_rows()
    by_destination = defaultdict(list)
    names = {}
    for r in rows:
        by_destination[r["destination"]].append(r)
        names[r["destination"]] = r["destination_name"]

    monthly = load_monthly()

    priority_cards = []
    for code in priority_codes:
        dest = destinations[code]
        if code in by_destination:
            meta, card = build_card(
                code,
                names[code],
                by_destination[code],
                True,
                color_coded=not dest.get("alerts", True),
                months=monthly.get(code),
                direct_only=dest.get("direct_only", False),
            )
            priority_cards.append((meta, card))
        else:
            floor = dest.get("floor_price_cad")
            extra = f" Floor price is set to ${floor:,.0f} CAD." if floor else ""
            priority_cards.append(
                ({"rank": 999.0, "price": 999999.0, "change": 999999.0, "name": dest["name"]},
                 NO_DATA_CARD_TEMPLATE.format(
                    name=html.escape(dest["name"]),
                    sort_name=html.escape(dest["name"]),
                    code=html.escape(code),
                    extra=extra,
                ))
            )

    other_codes = sorted(c for c in by_destination if c not in set(priority_codes))
    if other_codes:
        built = [
            build_card(
                code, names[code], by_destination[code], False, False,
                months=monthly.get(code),
                direct_only=destinations.get(code, {}).get("direct_only", False),
            )
            for code in other_codes
        ]
        built.sort(key=lambda pair: (pair[0]["rank"], pair[0]["name"]))
        other_cards_html = "\n".join(card for _, card in built)
    else:
        other_cards_html = '<p class="no-data">No price history yet &mdash; check back after the next run.</p>'

    priority_section = ""
    if priority_cards:
        priority_cards.sort(key=lambda pair: (pair[0]["rank"], pair[0]["name"]))
        priority_section = (
            '<h2 class="section">Priority routes</h2>\n'
            '<div class="card-list" id="priority-list">\n'
            + "\n".join(card for _, card in priority_cards)
            + "\n</div>"
        )

    cards_html = ""
    if other_codes or not priority_cards:
        heading = '<h2 class="section">All destinations</h2>\n' if priority_cards else ""
        cards_html = (
            heading
            + '<div class="card-list" id="all-list">\n'
            + other_cards_html
            + "\n</div>"
        )

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        f.write(
            PAGE_TEMPLATE.format(
                styles=STYLES,
                script=SCRIPT,
                sortbar=(SORTBAR_HTML + build_monthbar(monthly)) if by_destination else "",
                meta=build_meta(rows, len(by_destination)),
                priority_section=priority_section,
                cards=cards_html,
            )
        )

    print(f"Wrote {OUTPUT_PATH}")



STYLES += """
  .sortbar {
    display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap;
    margin: 0 0 2rem;
  }
  .sortbar .label {
    font-size: 0.66rem; text-transform: uppercase; letter-spacing: 0.09em;
    color: var(--ink-faint); margin-right: 0.35rem;
  }
  .sortbar button {
    font: inherit; font-size: 0.78rem; color: var(--ink-soft);
    background: transparent; border: 1px solid var(--hairline);
    border-radius: 999px; padding: 0.3rem 0.8rem; cursor: pointer;
    transition: background 0.12s ease, color 0.12s ease, border-color 0.12s ease;
  }
  .sortbar button:hover { color: var(--ink); border-color: var(--ink-faint); }
  .sortbar button[aria-pressed="true"] {
    background: var(--ink); color: var(--paper); border-color: var(--ink);
  }
  .sortbar button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  @media (max-width: 34rem) {
    .sortbar .label { width: 100%; margin-bottom: 0.15rem; }
  }
"""

SORTBAR_HTML = """<nav class="sortbar" aria-label="Sort routes">
  <span class="label">Sort</span>
  <button type="button" data-sort="rank" aria-pressed="true">Best signal</button>
  <button type="button" data-sort="price" aria-pressed="false">Lowest price</button>
  <button type="button" data-sort="change" aria-pressed="false">Biggest drop</button>
  <button type="button" data-sort="name" aria-pressed="false">A-Z</button>
</nav>
"""

SCRIPT = """
(function () {
  var sortbar = document.querySelector('.sortbar:not(.monthbar):not(.seasonbar)');
  var monthbar = document.querySelector('.monthbar');
  var seasonbar = document.querySelector('.seasonbar');
  var lists = document.querySelectorAll('.card-list');
  if (!sortbar && !monthbar) return;

  var SEASONS = {
    winter: [12, 1, 2], spring: [3, 4, 5],
    summer: [6, 7, 8], fall: [9, 10, 11]
  };

  function monthsOf(card) {
    try { return JSON.parse(card.dataset.months || '{}'); } catch (e) { return {}; }
  }

  function reorder(keyFn) {
    lists.forEach(function (list) {
      var cards = Array.prototype.slice.call(list.children);
      cards.sort(function (a, b) {
        var ka = keyFn(a), kb = keyFn(b);
        if (ka < kb) return -1;
        if (ka > kb) return 1;
        return a.dataset.name.localeCompare(b.dataset.name);
      });
      cards.forEach(function (card) { list.appendChild(card); });
    });
  }

  function press(bar, attr, value) {
    if (!bar) return;
    bar.querySelectorAll('button').forEach(function (btn) {
      btn.setAttribute('aria-pressed', String(btn.dataset[attr] === value));
    });
  }

  function sortBy(mode) {
    reorder(function (card) {
      if (mode === 'name') return card.dataset.name.toLowerCase();
      return parseFloat(card.dataset[mode]);
    });
    press(sortbar, 'sort', mode);
    try { localStorage.setItem('sortMode', mode); } catch (e) {}
  }

  // `match` decides which departure months are in scope. Selected months are
  // highlighted on every chart, routes with none are dimmed, and cards are
  // ordered by their cheapest fare within the selection.
  function apply(match) {
    document.querySelectorAll('.card').forEach(function (card) {
      var hit = false;
      card.querySelectorAll('.month-group').forEach(function (group) {
        var picked = match(group.dataset.month);
        if (picked) hit = true;
        group.classList.toggle('picked', picked);
        group.classList.toggle('dim', !picked);
      });
      card.classList.toggle('no-month-data', !hit);
    });
    reorder(function (card) {
      var data = monthsOf(card), best = Infinity;
      Object.keys(data).forEach(function (key) {
        if (match(key) && data[key] < best) best = data[key];
      });
      return best;
    });
    press(sortbar, 'sort', null);
  }

  function clearMonths() {
    document.querySelectorAll('.month-group').forEach(function (group) {
      group.classList.remove('picked', 'dim');
    });
    document.querySelectorAll('.card').forEach(function (card) {
      card.classList.remove('no-month-data');
    });
  }

  function showAll() {
    clearMonths();
    press(monthbar, 'month', 'all');
    press(seasonbar, 'season', null);
    var saved = 'rank';
    try { saved = localStorage.getItem('sortMode') || 'rank'; } catch (e) {}
    sortBy(saved);
    try { localStorage.removeItem('travelPick'); } catch (e) {}
  }

  function pickMonth(month) {
    if (month === 'all') return showAll();
    apply(function (key) { return key === month; });
    press(monthbar, 'month', month);
    press(seasonbar, 'season', null);
    try { localStorage.setItem('travelPick', 'month:' + month); } catch (e) {}
  }

  function pickSeason(season) {
    var numbers = SEASONS[season] || [];
    apply(function (key) { return numbers.indexOf(parseInt(key.slice(5, 7), 10)) !== -1; });
    press(seasonbar, 'season', season);
    press(monthbar, 'month', null);
    try { localStorage.setItem('travelPick', 'season:' + season); } catch (e) {}
  }

  if (sortbar) {
    sortbar.addEventListener('click', function (e) {
      var btn = e.target.closest('button[data-sort]');
      if (btn) { showAll(); sortBy(btn.dataset.sort); }
    });
  }
  if (monthbar) {
    monthbar.addEventListener('click', function (e) {
      var btn = e.target.closest('button[data-month]');
      if (btn) pickMonth(btn.dataset.month);
    });
  }
  if (seasonbar) {
    seasonbar.addEventListener('click', function (e) {
      var btn = e.target.closest('button[data-season]');
      if (btn) pickSeason(btn.dataset.season);
    });
  }

  try {
    var savedSort = localStorage.getItem('sortMode');
    if (savedSort && savedSort !== 'rank') sortBy(savedSort);
    var pick = localStorage.getItem('travelPick');
    if (pick && pick.indexOf('month:') === 0 && monthbar &&
        monthbar.querySelector('[data-month="' + pick.slice(6) + '"]')) {
      pickMonth(pick.slice(6));
    } else if (pick && pick.indexOf('season:') === 0 && seasonbar &&
        seasonbar.querySelector('[data-season="' + pick.slice(7) + '"]')) {
      pickSeason(pick.slice(7));
    }
  } catch (e) {}
})();
"""


def build_monthbar(monthly):
    """Season and travel-month filters, covering every month any route priced."""
    months = sorted({m for route in monthly.values() for m in route})
    if not months:
        return ""

    present = {int(m[5:7]) for m in months}
    seasons = [
        ("winter", "Winter", (12, 1, 2)),
        ("spring", "Spring", (3, 4, 5)),
        ("summer", "Summer", (6, 7, 8)),
        ("fall", "Fall", (9, 10, 11)),
    ]
    season_buttons = [
        f'<button type="button" data-season="{key}" aria-pressed="false">{label}</button>'
        for key, label, numbers in seasons
        # Only offer a season we actually hold months for.
        if present & set(numbers)
    ]

    total_routes = len(monthly)
    month_buttons = [
        '<button type="button" data-month="all" aria-pressed="true">All months</button>'
    ]
    for month in months:
        try:
            label = datetime.strptime(month, "%Y-%m").strftime("%b %Y")
        except ValueError:
            label = month
        # Travelpayouts' cache thins out the further ahead you look, so show how
        # many routes a month actually covers before it is clicked.
        count = sum(1 for route in monthly.values() if month in route)
        sparse = " sparse" if total_routes and count <= total_routes * 0.25 else ""
        month_buttons.append(
            f'<button type="button" class="month-btn{sparse}" data-month="{month}" '
            f'aria-pressed="false">{html.escape(label)}'
            f'<span class="count">{count}</span></button>'
        )

    out = (
        '<nav class="sortbar monthbar" aria-label="Filter by travel month">\n'
        '  <span class="label">Travel month</span>\n  '
        + "\n  ".join(month_buttons)
        + "\n</nav>\n"
    )
    if season_buttons:
        out += (
            '<nav class="sortbar seasonbar" aria-label="Filter by season">\n'
            '  <span class="label">Season</span>\n  '
            + "\n  ".join(season_buttons)
            + "\n</nav>\n"
        )
    return out


STYLES += """
  .months {
    border-top: 1px solid var(--hairline-soft);
    margin: 0 0 0.25rem; padding-top: 0.9rem;
  }
  .months-head {
    display: flex; align-items: baseline; gap: 0.6rem;
    flex-wrap: wrap; margin-bottom: 0.2rem;
  }
  .months-title {
    font-size: 0.66rem; text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--ink-faint);
  }
  .months-range { font-size: 0.72rem; color: var(--ink-faint); }
  .months-summary { font-size: 0.78rem; color: var(--ink-soft); margin-left: auto; }
  .months-chart { display: block; width: 100%; height: auto; }
  .months-chart .bar { fill: var(--ink-faint); opacity: 0.28; }
  .months-chart .bar-low { fill: var(--good); opacity: 1; }
  .months-chart .bar-price {
    font-size: 11px; fill: var(--ink-faint);
    font-variant-numeric: tabular-nums;
    font-family: ui-sans-serif, -apple-system, sans-serif;
  }
  .months-chart .bar-price-low { fill: var(--good); font-weight: 600; }
  .months-chart .bar-label {
    font-size: 10px; fill: var(--ink-faint); letter-spacing: 0.04em;
    font-family: ui-sans-serif, -apple-system, sans-serif;
  }
  .months-chart .bar-label-low { fill: var(--good); font-weight: 600; }
  .months-chart .month-group.dim { opacity: 0.35; }
  .months-chart .month-group.picked .bar { fill: var(--accent); opacity: 1; }
  .months-chart .month-group.picked .bar-price,
  .months-chart .month-group.picked .bar-label { fill: var(--accent); font-weight: 600; }
  .monthbar, .seasonbar { margin-top: -1.2rem; }
  .sortbar .count {
    display: inline-block; margin-left: 0.4rem; padding: 0 0.32rem;
    font-size: 0.68rem; font-variant-numeric: tabular-nums;
    background: var(--hairline-soft); color: var(--ink-faint); border-radius: 4px;
  }
  .sortbar button[aria-pressed="true"] .count {
    background: rgba(255, 255, 255, 0.22); color: inherit;
  }
  .sortbar button.sparse { color: var(--ink-faint); border-style: dashed; }
  .card.no-month-data { opacity: 0.4; }
  @media (max-width: 34rem) {
    .months-summary { margin-left: 0; width: 100%; }
  }
"""

if __name__ == "__main__":
    main()
