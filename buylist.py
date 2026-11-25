"""Show the buy list: which items are on, the most you can pay, and Terapeak links.

    python buylist.py            print the table
    python buylist.py --md       also write BUYLIST.md (open it on GitHub to click the links)
"""

import sys
from urllib.parse import quote_plus

import hunter as h


def terapeak_url(it):
    kw = it.get("terapeak") or it["query"]
    return ("https://www.ebay.com/sh/research?marketplace=EBAY-US&tabName=SOLD&dayRange=90&keywords="
            + quote_plus(str(kw)))


def rows(cfg):
    s = cfg["settings"]
    out = []
    for it in cfg["items"]:
        row = {"name": it["name"], "on": bool(it["sold"]), "link": terapeak_url(it)}
        for cond in ("used", "new"):
            r = it["sold"].get(cond)
            row[cond] = r
            # assumes typical $8 shipping to you
            row[cond + "_max"] = round(h.max_price(r, 8.0, s), 2) if r else None
        out.append(row)
    return out


def fmt(v):
    return f"${v:.0f}" if v is not None else "-"


def main():
    cfg = h.load_buylist()
    s = cfg["settings"]
    table = rows(cfg)
    on = sum(r["on"] for r in table)
    print(f"{on} of {len(table)} items on. Target profit ${s['target_profit']:.0f}.")
    print(f"An item can only make the target if it resells for at least "
          f"${breakeven(s):.0f} (that's with $0 paid and $8 shipping in).\n")
    print(f"{'item':34} {'used':>6} {'max buy':>8} {'new':>6} {'max buy':>8}")
    for r in table:
        print(f"{r['name'][:34]:34} {fmt(r['used']):>6} {fmt(r['used_max']):>8} {fmt(r['new']):>6} {fmt(r['new_max']):>8}")
    if "--md" in sys.argv:
        lines = ["# Buy list", "",
                 "Max buy = most you can pay for the item (before shipping and tax) and still make "
                 f"${s['target_profit']:.0f}, assuming $8 shipping to you.", "",
                 "| Item | On | Used sold | Used max buy | New sold | New max buy | Terapeak |",
                 "|---|---|---|---|---|---|---|"]
        for r in table:
            lines.append(f"| {r['name']} | {'yes' if r['on'] else 'no'} | {fmt(r['used'])} | {fmt(r['used_max'])} "
                         f"| {fmt(r['new'])} | {fmt(r['new_max'])} | [sold 90 days]({r['link']}) |")
        (h.HERE / "BUYLIST.md").write_text("\n".join(lines) + "\n")
        print("\nWrote BUYLIST.md")


def breakeven(s):
    """Lowest resale price where target profit is possible at all."""
    lo, hi = 0.0, 1000.0
    for _ in range(50):
        mid = (lo + hi) / 2
        if h.max_price(mid, 8.0, s) >= 0:
            hi = mid
        else:
            lo = mid
    return hi


if __name__ == "__main__":
    main()
