"""Show the buy list: which items are on, the most you can pay, and Terapeak links.

    python buylist.py                       print the table
    python buylist.py --md                  also write BUYLIST.md (open it on GitHub to click the links)
    python buylist.py --group shorts        only items in one group
    python buylist.py --targets 20,15,10    show the most you can pay at each profit target

Each item uses its own costs (see "costs:" and "groups:" in buylist.yaml).
"""

import sys
from urllib.parse import quote_plus

import hunter as h


def terapeak_url(it):
    kw = it.get("terapeak") or it["query"]
    return ("https://www.ebay.com/sh/research?marketplace=EBAY-US&tabName=SOLD&dayRange=90&keywords="
            + quote_plus(str(kw)))


def rows(cfg, group=None, target=None):
    out = []
    for it in cfg["items"]:
        if group and it.get("group") != group:
            continue
        s = dict(it["s"])
        if target is not None:
            s["target_profit"] = target
        row = {"name": it["name"], "on": bool(it["sold"]), "link": terapeak_url(it),
               "ship": s["typical_ship_in"], "target": s["target_profit"]}
        for cond in ("used", "new"):
            r = it["sold"].get(cond)
            row[cond] = r
            # uses the item's typical shipping to you
            row[cond + "_max"] = round(h.max_price(r, s["typical_ship_in"], s), 2) if r else None
        out.append(row)
    return out


def fmt(v):
    return f"${v:.0f}" if v is not None else "-"


def arg_after(flag):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None


def main():
    cfg = h.load_buylist()
    s = cfg["settings"]
    group = arg_after("--group")
    targets = arg_after("--targets")
    if targets:
        # one block per profit target, using each item's own costs
        goals = [float(x) for x in targets.split(",")]
        base = rows(cfg, group)
        print(f"Most you can pay (item price, before shipping and tax) at each profit target. "
              f"Group: {group or 'all'}.\n")
        head = f"{'item':34}" + "".join(f"{'used $' + format(g, 'g'):>10}{'new $' + format(g, 'g'):>10}" for g in goals)
        print(head)
        for i, b in enumerate(base):
            line = f"{b['name'][:34]:34}"
            for g in goals:
                r = rows(cfg, group, g)[i]
                line += f"{fmt(r['used_max']):>10}{fmt(r['new_max']):>10}"
            print(line)
        return
    table = rows(cfg, group)
    on = sum(r["on"] for r in table)
    print(f"{on} of {len(table)} items on. Default target profit ${s['target_profit']:.0f}.")
    print(f"With default costs, an item needs to resell for at least ${breakeven(s):.0f} to make the target "
          f"(that's with $0 paid and ${s['typical_ship_in']:.0f} shipping in). Items in a group use their own costs.\n")
    print(f"{'item':34} {'target':>6} {'used':>6} {'max buy':>8} {'new':>6} {'max buy':>8}")
    for r in table:
        print(f"{r['name'][:34]:34} {'$' + format(r['target'], 'g'):>6} {fmt(r['used']):>6} {fmt(r['used_max']):>8} {fmt(r['new']):>6} {fmt(r['new_max']):>8}")
    if "--md" in sys.argv:
        lines = ["# Buy list", "",
                 "Max buy = most you can pay for the item (before shipping and tax) and still make "
                 "the item's target profit, using the item's own costs and its typical shipping to you.", "",
                 "| Item | On | Target | Used sold | Used max buy | New sold | New max buy | Terapeak |",
                 "|---|---|---|---|---|---|---|---|"]
        for r in table:
            lines.append(f"| {r['name']} | {'yes' if r['on'] else 'no'} | ${r['target']:g} | {fmt(r['used'])} | {fmt(r['used_max'])} "
                         f"| {fmt(r['new'])} | {fmt(r['new_max'])} | [sold 90 days]({r['link']}) |")
        (h.HERE / "BUYLIST.md").write_text("\n".join(lines) + "\n")
        print("\nWrote BUYLIST.md")


def breakeven(s):
    """Lowest resale price where target profit is possible at all (with the default costs)."""
    lo, hi = 0.0, 1000.0
    for _ in range(50):
        mid = (lo + hi) / 2
        if h.max_price(mid, s['typical_ship_in'], s) >= 0:
            hi = mid
        else:
            lo = mid
    return hi


if __name__ == "__main__":
    main()
