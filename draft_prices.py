"""One-off: draft sold prices for buylist.yaml from eBay data.

Tries eBay's sold-listings API (Marketplace Insights) first. Most developer
keys don't have it, so it falls back to current Buy It Now asking prices,
which run above sold prices. Use the low end (25th percentile) as the draft
and confirm in Terapeak. Writes DRAFT_PRICES.md.
"""

import base64
import os
import statistics

import requests

import hunter as h

INSIGHTS_URL = "https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search"
INSIGHTS_SCOPE = "https://api.ebay.com/oauth/api_scope/buy.marketplace.insights"


def insights_token():
    cid = "".join(os.environ["EBAY_CLIENT_ID"].split())
    sec = "".join(os.environ["EBAY_CLIENT_SECRET"].split())
    auth = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    r = requests.post(h.TOKEN_URL, timeout=30, headers={
        "Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": INSIGHTS_SCOPE})
    if r.status_code != 200:
        return None, f"no access ({r.status_code}: {r.text[:120]})"
    return r.json()["access_token"], "ok"


def pct(vals, p):
    v = sorted(vals)
    k = (len(v) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def clean_prices(listings, it, cfg):
    out = []
    for x in listings:
        tn = h.norm(x.get("title"))
        if h.is_lot(tn) or not h.title_ok(tn, it, cfg):
            continue
        p = h.money(x.get("price")) or h.money(x.get("lastSoldPrice"))
        if p and p >= 10:
            out.append(p)
    return out


def summary(prices):
    if len(prices) < 8:
        return {"n": len(prices), "p25": None, "med": None}
    return {"n": len(prices), "p25": round(pct(prices, 0.25)), "med": round(statistics.median(prices))}


def main():
    cfg = h.load_buylist()
    ebay = h.Ebay(os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"])
    itok, status = insights_token()
    print("Sold-listings API:", status)
    rows = []
    for it in cfg["items"]:
        row = {"name": it["name"]}
        for cond, cf in (("used", "conditions:{USED}"), ("new", "conditions:{NEW}")):
            filters = ["itemLocationCountry:US", "priceCurrency:USD", cf]
            sold = None
            if itok:
                r = requests.get(INSIGHTS_URL, timeout=30, params={"q": it["query"], "limit": "200", "filter": ",".join(filters)},
                                 headers={"Authorization": "Bearer " + itok, "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
                if r.status_code == 200:
                    sold = summary(clean_prices(r.json().get("itemSales") or [], it, cfg))
            ask = summary(clean_prices(ebay.search(it["query"], filters + ["buyingOptions:{FIXED_PRICE}"], "newlyListed", limit=200), it, cfg))
            row[cond] = {"sold": sold, "ask": ask}
        rows.append(row)
        print(row, flush=True)

    def cell(d):
        if not d:
            return "-"
        return f"{d['n']} / ${d['p25']} / ${d['med']}" if d["p25"] else f"{d['n']} (too few)"

    lines = ["# Draft prices", "", f"Sold-listings API: {status}", "",
             "Each cell: listings found / 25th percentile / median.", "",
             "| Item | Used sold | Used asking | New sold | New asking |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['name']} | {cell(r['used']['sold'])} | {cell(r['used']['ask'])} "
                     f"| {cell(r['new']['sold'])} | {cell(r['new']['ask'])} |")
    (h.HERE / "DRAFT_PRICES.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
