"""Weekly scorecard from alerts.csv: how close are we to 50 deals a week?

    python report.py
"""

from collections import defaultdict
from datetime import datetime

import hunter as h

GOAL = 50


def week_of(ts):
    d = datetime.strptime(ts[:10], "%Y-%m-%d")
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def main():
    rows = h.read_alerts()
    if not rows:
        print("No alerts logged yet.")
        return
    cfg = h.load_buylist()
    s = cfg["settings"]
    own = {it["name"]: it["s"] for it in cfg["items"]}  # each item's own costs
    weeks = defaultdict(lambda: {"alerts": 0, "deals": 0, "bought": 0, "wrong": 0, "est": 0.0, "real": 0.0, "sold": 0})
    for r in rows:
        w = weeks[week_of(r["sent_at"])]
        w["alerts"] += 1
        est = float(r["profit_est"] or 0)
        n = int(r.get("count") or 1)
        si = own.get(r["item"], s)
        w["deals"] += h.deal_count(r, si)  # a lot can count for more than one deal
        if r.get("outcome") == "bought":
            w["bought"] += 1
            w["est"] += est
            if r.get("resale_actual"):
                real = h.net_total(float(r["resale_actual"]) / n, n, si) - float(r["landed"])
                w["real"] += real
                w["sold"] += 1
        elif r.get("outcome") == "wrong":
            w["wrong"] += 1
    print(f"| Week | Alerts | Deals (goal {GOAL}) | Bought | Marked wrong | Est. profit bought | Resold | Real profit |")
    print("|---|---|---|---|---|---|---|---|")
    for k in sorted(weeks):
        w = weeks[k]
        print(f"| {k} | {w['alerts']} | {w['deals']} | {w['bought']} | {w['wrong']} | ${w['est']:.0f} | {w['sold']} | ${w['real']:.0f} |")


if __name__ == "__main__":
    main()
