#!/usr/bin/env python3
"""Build docs/history.html from data/history.csv."""

import csv
import html
import os
from collections import defaultdict

HISTORY_PATH = os.path.join(os.path.dirname(__file__), "data", "history.csv")
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "docs", "history.html")

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Flight Price History — YYZ to Europe</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 960px;
         margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; background: #fafafa; }}
  h1 {{ font-size: 1.5rem; }}
  .updated {{ color: #666; font-size: 0.9rem; margin-bottom: 2rem; }}
  .card {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
          padding: 1rem 1.25rem; margin-bottom: 1.5rem; }}
  .card h2 {{ margin: 0 0 0.5rem; font-size: 1.15rem; }}
  .stats {{ display: flex; gap: 1.5rem; flex-wrap: wrap; margin-bottom: 0.75rem;
           font-size: 0.9rem; }}
  .stats div {{ background: #f0f4ff; border-radius: 6px; padding: 0.4rem 0.75rem; }}
  .stats b {{ display: block; font-size: 1.1rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th, td {{ text-align: left; padding: 0.35rem 0.5rem; border-bottom: 1px solid #eee; }}
  tr.cheapest {{ background: #e6f9ec; font-weight: 600; }}
  details summary {{ cursor: pointer; color: #444; font-size: 0.9rem; margin-top: 0.5rem; }}
</style>
</head>
<body>
<h1>Flight Price History — YYZ to Europe</h1>
<p class="updated">Last updated: {updated}</p>
{cards}
</body>
</html>
"""

CARD_TEMPLATE = """<div class="card">
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
      <tr><th>Checked</th><th>Price</th><th>Depart</th><th>Return</th></tr>
      {rows}
    </table>
  </details>
</div>
"""


def load_rows():
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH, newline="") as f:
        return list(csv.DictReader(f))


def build_card(code, name, rows):
    rows_sorted = sorted(rows, key=lambda r: r["checked_at"])
    prices = [float(r["price"]) for r in rows_sorted]
    lowest = min(prices)
    latest = prices[-1]
    avg = sum(prices) / len(prices)

    table_rows = []
    for r in reversed(rows_sorted):
        cls = ' class="cheapest"' if float(r["price"]) == lowest else ""
        table_rows.append(
            f"<tr{cls}><td>{html.escape(r['checked_at'][:10])}</td>"
            f"<td>${float(r['price']):.0f}</td>"
            f"<td>{html.escape(r['depart_date'])}</td>"
            f"<td>{html.escape(r['return_date'])}</td></tr>"
        )

    return CARD_TEMPLATE.format(
        name=html.escape(name),
        code=html.escape(code),
        lowest=lowest,
        latest=latest,
        avg=avg,
        count=len(rows_sorted),
        rows="\n      ".join(table_rows),
    )


def main():
    rows = load_rows()
    by_destination = defaultdict(list)
    names = {}
    for r in rows:
        by_destination[r["destination"]].append(r)
        names[r["destination"]] = r["destination_name"]

    if not by_destination:
        cards_html = "<p>No price history yet — check back after the next run.</p>"
        updated = "never"
    else:
        cards_html = "\n".join(
            build_card(code, names[code], by_destination[code])
            for code in sorted(by_destination)
        )
        updated = max(r["checked_at"] for r in rows)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        f.write(PAGE_TEMPLATE.format(updated=html.escape(updated), cards=cards_html))

    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
