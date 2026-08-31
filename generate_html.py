#!/usr/bin/env python3
"""Build docs/history.html from data/history.csv."""

import csv
import html
import os
from collections import defaultdict

import yaml

HISTORY_PATH = os.path.join(os.path.dirname(__file__), "data", "history.csv")
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "docs", "history.html")

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Flight Price History — YYZ</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 960px;
         margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; background: #fafafa; }}
  h1 {{ font-size: 1.5rem; }}
  h2.section {{ font-size: 1.1rem; color: #444; margin: 2rem 0 1rem; }}
  .updated {{ color: #666; font-size: 0.9rem; margin-bottom: 2rem; }}
  .card {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
          padding: 1rem 1.25rem; margin-bottom: 1.5rem; }}
  .card h2 {{ margin: 0 0 0.5rem; font-size: 1.15rem; }}
  .card.priority {{ background: #fffaf0; border: 2px solid #e0a030; }}
  .priority-badge {{ display: inline-block; background: #e0a030; color: #fff;
                     font-size: 0.7rem; font-weight: 700; letter-spacing: 0.03em;
                     text-transform: uppercase; padding: 0.2rem 0.5rem;
                     border-radius: 4px; margin-bottom: 0.5rem; }}
  .stats {{ display: flex; gap: 1.5rem; flex-wrap: wrap; margin-bottom: 0.75rem;
           font-size: 0.9rem; }}
  .stats div {{ background: #f0f4ff; border-radius: 6px; padding: 0.4rem 0.75rem; }}
  .card.priority .stats div {{ background: #fdf0d5; }}
  .stats b {{ display: block; font-size: 1.1rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th, td {{ text-align: left; padding: 0.35rem 0.5rem; border-bottom: 1px solid #eee; }}
  tr.cheapest {{ background: #e6f9ec; font-weight: 600; }}
  details summary {{ cursor: pointer; color: #444; font-size: 0.9rem; margin-top: 0.5rem; }}
  .no-data {{ color: #888; font-style: italic; }}
</style>
</head>
<body>
<h1>Flight Price History — YYZ</h1>
<p class="updated">Last updated: {updated}</p>
{priority_section}
{cards}
</body>
</html>
"""

CARD_TEMPLATE = """<div class="card{priority_class}">
  {badge}
  <h2>{name} ({code})</h2>
  <div class="stats">
    <div><b>${lowest:.0f}</b>lowest ever</div>
    <div><b>${latest:.0f}</b>latest</div>
    <div><b>${avg:.0f}</b>all-time avg</div>
    <div><b>{count}</b>checks</div>
  </div>
  <details>
    <summary>Full history ({count} checks)</summary>
    <table>
      <tr><th>Checked</th><th>Price</th><th>Depart</th><th>Return</th><th>Flight</th></tr>
      {rows}
    </table>
  </details>
</div>
"""

NO_DATA_CARD_TEMPLATE = """<div class="card priority">
  <span class="priority-badge">Priority route</span>
  <h2>{name} ({code})</h2>
  <p class="no-data">No fare data cached yet for this route — it'll appear
  here once Travelpayouts has a price to report. Floor price is set to
  ${floor:.0f} CAD.</p>
</div>
"""


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_rows():
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH, newline="") as f:
        return list(csv.DictReader(f))


def build_card(code, name, rows, is_priority):
    rows_sorted = sorted(rows, key=lambda r: r["checked_at"])
    prices = [float(r["price"]) for r in rows_sorted]
    lowest = min(prices)
    latest = prices[-1]
    avg = sum(prices) / len(prices)

    table_rows = []
    for r in reversed(rows_sorted):
        cls = ' class="cheapest"' if float(r["price"]) == lowest else ""
        airline = (r.get("airline_name") or r.get("airline") or "").strip()
        flight_number = (r.get("flight_number") or "").strip()
        flight = f"{airline} {flight_number}".strip() or "—"
        table_rows.append(
            f"<tr{cls}><td>{html.escape(r['checked_at'][:10])}</td>"
            f"<td>${float(r['price']):.0f}</td>"
            f"<td>{html.escape(r['depart_date'])}</td>"
            f"<td>{html.escape(r['return_date'])}</td>"
            f"<td>{html.escape(flight)}</td></tr>"
        )

    return CARD_TEMPLATE.format(
        name=html.escape(name),
        code=html.escape(code),
        lowest=lowest,
        latest=latest,
        avg=avg,
        count=len(rows_sorted),
        rows="\n      ".join(table_rows),
        priority_class=" priority" if is_priority else "",
        badge='<span class="priority-badge">Priority route</span>' if is_priority else "",
    )


def main():
    config = load_config()
    priority = {
        d["code"]: d
        for d in config.get("destinations", [])
        if d.get("priority")
    }
    default_floor = config.get("deal_rules", {}).get("floor_price_cad", 0)

    rows = load_rows()
    by_destination = defaultdict(list)
    names = {}
    for r in rows:
        by_destination[r["destination"]].append(r)
        names[r["destination"]] = r["destination_name"]

    priority_cards = []
    for code, dest in priority.items():
        if code in by_destination:
            priority_cards.append(build_card(code, names[code], by_destination[code], True))
        else:
            floor = dest.get("floor_price_cad", default_floor)
            priority_cards.append(
                NO_DATA_CARD_TEMPLATE.format(name=html.escape(dest["name"]), code=code, floor=floor)
            )

    other_codes = [c for c in by_destination if c not in priority]
    if other_codes:
        other_cards_html = "\n".join(
            build_card(code, names[code], by_destination[code], False)
            for code in sorted(other_codes)
        )
    else:
        other_cards_html = "<p>No price history yet — check back after the next run.</p>"

    priority_section = ""
    if priority_cards:
        priority_section = (
            '<h2 class="section">Priority routes</h2>\n' + "\n".join(priority_cards)
        )

    cards_html = ""
    if other_codes or not priority_cards:
        heading = '<h2 class="section">All destinations</h2>\n' if priority_cards else ""
        cards_html = heading + other_cards_html

    updated = max((r["checked_at"] for r in rows), default="never")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        f.write(
            PAGE_TEMPLATE.format(
                updated=html.escape(updated),
                priority_section=priority_section,
                cards=cards_html,
            )
        )

    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
