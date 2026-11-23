"""Spyhopper deal scanner, version 2.

How it works
  1. For every search in config.yaml it learns the going price on eBay from
     current Buy It Now listings (cached for a day in market.json).
  2. It then checks new Buy It Now listings and auctions ending soon for the
     same search, including lots.
  3. Anything priced well under the going price that still clears your
     minimum profit after fees and shipping is sent to ntfy on your phone.

Secrets (GitHub repo settings): EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, NTFY_TOPIC
Optional: DRY_RUN=1 prints deals instead of sending them.
"""

import base64
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.yaml"
SEEN_PATH = HERE / "seen.json"
MARKET_PATH = HERE / "market.json"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
NTFY_URL = "https://ntfy.sh/"
LOT_WORDS = ["lot", "bundle", "wholesale", "bulk"]


# ---------- helpers ----------

def norm(text):
    text = (text or "").lower().replace("'", "").replace("’", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " " + " ".join(text.split()) + " "


def has(title_n, term):
    return norm(term) in title_n


def any_word(title_n, words):
    return any(has(title_n, w) for w in words or [])


def money(obj):
    try:
        return float(obj["value"])
    except (TypeError, KeyError, ValueError):
        return None


LOT_PATTERNS = [
    r"\blot of (\d{1,3})\b",
    r"\b(\d{1,3}) ?(?:pairs?|pcs?|pieces?|items?|shorts|shirts|pants)\b",
    r"\blot (?:of )?\(?(\d{1,3})\)?\b",
    r"\b(\d{1,3}) ?x\b",
    r"\bx ?(\d{1,3})\b",
    r"\bset of (\d{1,3})\b",
]


def lot_count(title, max_lot):
    t = title.lower()
    for pat in LOT_PATTERNS:
        m = re.search(pat, t)
        if m:
            n = int(m.group(1))
            if 2 <= n <= max_lot:
                return n
    return None


def is_lot(title_n):
    return any_word(title_n, LOT_WORDS)


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def load_config(path=CONFIG_PATH):
    cfg = yaml.safe_load(Path(path).read_text())
    cfg.setdefault("exclude_words", [])
    cfg["exclude_words"] = [str(w) for w in cfg["exclude_words"] or []]
    for w in cfg["watch"]:
        w["query"] = str(w["query"])
        w.setdefault("name", w["query"])
        w.setdefault("condition", "used")
        w["must"] = [[str(t) for t in (g if isinstance(g, list) else [g])] for g in (w.get("must") or [])]
        w["skip"] = [str(t) for t in (w.get("skip") or [])]
    return cfg


# ---------- eBay ----------

def get_token(client_id, client_secret):
    client_id = "".join(client_id.split())
    client_secret = "".join(client_secret.split())
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    r = requests.post(
        TOKEN_URL,
        headers={"Authorization": "Basic " + auth,
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials",
              "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"eBay login failed {r.status_code}: {r.text[:300]}")
        sys.exit(1)
    return r.json()["access_token"]


CALLS = {"n": 0}


def search(token, q, filters, sort, category, limit=100):
    params = {"q": q, "limit": str(limit), "filter": ",".join(filters)}
    if category:
        params["category_ids"] = category
    if sort:
        params["sort"] = sort
    for attempt in range(3):
        CALLS["n"] += 1
        r = requests.get(
            SEARCH_URL, params=params, timeout=30,
            headers={"Authorization": "Bearer " + token,
                     "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        )
        if r.status_code == 200:
            return r.json().get("itemSummaries", []) or []
        if r.status_code in (429, 500, 502, 503):
            time.sleep(2 + attempt * 3)
            continue
        print(f"  search failed ({r.status_code}) for '{q}': {r.text[:200]}")
        return []
    return []


def condition_filter(cond):
    return {"new": ["conditions:{NEW}"], "used": ["conditions:{USED}"]}.get(cond, [])


STOP = {"men", "mens", "man", "in", "made", "the", "and", "of", "for", "a"}


def query_words_ok(title_n, query):
    """Every real word of the search must be in the title.
    eBay's search is loose and returns lots of near matches, so we check
    the title ourselves. 'boardshorts' counts for 'board shorts'."""
    squashed = title_n.replace(" ", "")
    for word in norm(query).split():
        if word in STOP:
            continue
        if f" {word} " in title_n:
            continue
        if len(word) > 3 and word in squashed:
            continue
        return False
    return True


LOOKALIKE = re.compile(r" like (?!new )| vibes? | aesthetic | esque | type (?![0-9ivx]+ )| comparable | similar ")


def title_ok(title_n, w, cfg, query=None):
    if any_word(title_n, cfg["exclude_words"]) or any_word(title_n, w["skip"]):
        return False
    # "RRL like", "like OP", "Patagonia vibes" and similar mean it is not that brand
    if LOOKALIKE.search(title_n):
        return False
    if not query_words_ok(title_n, query or w["query"]):
        return False
    return all(any_word(title_n, group) for group in w["must"])


# ---------- market price ----------

def percentile(values, p):
    values = sorted(values)
    if not values:
        return None
    k = (len(values) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def market_price(token, w, cfg, market, now):
    s = cfg["settings"]
    vq = w.get("value_query") or w["query"]
    key = "v2|" + vq + "|" + w["condition"]
    cached = market.get(key)
    if cached and now.timestamp() - cached["at"] < s["market_refresh_hours"] * 3600:
        return cached.get("price")
    items = search(token, vq, ["buyingOptions:{FIXED_PRICE}", "priceCurrency:USD",
                                       "itemLocationCountry:US"] + condition_filter(w["condition"]),
                   None, s["category_id"], limit=200)
    prices = []
    for it in items:
        tn = norm(it.get("title"))
        if is_lot(tn) or not title_ok(tn, w, cfg, vq):
            continue
        p = money(it.get("price"))
        if p and p >= 5:
            prices.append(p)
    price = None
    if len(prices) >= s["market_min_samples"]:
        # Asking prices run above sold prices, so use a low percentile
        # and then a safety discount.
        price = round(percentile(prices, s["market_percentile"]) * s["market_discount"], 2)
    market[key] = {"at": now.timestamp(), "price": price, "samples": len(prices),
                   "median_ask": round(statistics.median(prices), 2) if prices else None}
    return price


# ---------- ShopGoodwill (lots only) ----------

SGW_URL = "https://buyerapi.shopgoodwill.com/api/Search/ItemListing"


def search_sgw(text):
    body = {"isAdvancedSearch": False, "isWeddingCatagory": "false", "isMultipleCategoryIds": False,
            "isFromHeaderMenuTab": False, "layout": "grid", "searchText": text, "selectedGroup": "",
            "selectedCategoryIds": "", "selectedSellerIds": "", "lowPrice": "0", "highPrice": "999999",
            "searchBuyNowOnly": "", "searchPickupOnly": "false", "searchNoPickupOnly": "false",
            "searchOneCentShippingOnly": "false", "searchDescriptions": "false",
            "searchClosedAuctions": "false", "closedAuctionEndingDate": "", "closedAuctionDaysBack": "7",
            "searchCanadaShipping": "false", "searchInternationalShippingOnly": "false",
            "sortColumn": "1", "page": "1", "pageSize": "40", "sortDescending": "false",
            "savedSearchId": 0, "useBuyerPrefs": "true", "searchUSOnlyShipping": "false",
            "categoryLevelNo": "1", "partNumber": "", "catIds": ""}
    try:
        r = requests.post(SGW_URL, json=body, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
            "Origin": "https://shopgoodwill.com", "Referer": "https://shopgoodwill.com/"})
        if r.status_code != 200:
            print(f"  ShopGoodwill search failed ({r.status_code}) for '{text}'")
            return []
        return (r.json().get("searchResults") or {}).get("items") or []
    except Exception as e:
        print(f"  ShopGoodwill error for '{text}': {e}")
        return []


def evaluate_sgw(item, w, single_value, cfg, now):
    s = cfg["settings"]
    title = item.get("title") or ""
    tn = norm(title)
    if not title_ok(tn, w, cfg) or not is_lot(tn):
        return None
    if any_word(tn, ["for parts", "as is", "damaged", "stain", "stained", "holes"]):
        return None
    n = lot_count(title, s["max_lot_size"])
    if not n:
        return None
    try:
        end = datetime.fromisoformat(item["endTime"]).replace(tzinfo=timezone(timedelta(hours=-8)))
    except Exception:
        return None
    hours_left = (end - now).total_seconds() / 3600
    if hours_left < 0 or hours_left > s.get("sgw_hours_left", 6):
        return None
    price = float(item.get("currentPrice") or 0)
    if price <= 0:
        return None
    ship_in = s.get("sgw_shipping", 15.0)
    cost = price + ship_in
    per_piece = round(single_value * s["lot_piece_factor"], 2)
    resale = per_piece * n
    rate = s["fee_rate"] + s["promo_rate"]
    fees = (resale + s["buyer_shipping"] * n) * rate + s["fee_fixed"] * n
    profit = round(resale - fees - cost, 2)
    if profit < s["min_profit"] or cost / n > single_value * s["max_cost_ratio"]:
        return None
    iid = f"sgw|{item.get('itemId')}"
    return {"id": iid, "title": title, "url": f"https://shopgoodwill.com/item/{item.get('itemId')}",
            "search": "ShopGoodwill: " + w["name"], "mode": "sgw", "price": price, "ship_in": ship_in,
            "ship_guess": True, "count": n, "per_piece": per_piece, "resale": round(resale, 2),
            "profit": profit, "condition": None, "ends": end.astimezone(timezone.utc).isoformat(),
            "hours_left": round(hours_left, 1), "bids": item.get("numBids", 0)}


# ---------- deals ----------

def ship_cost(item, s):
    opts = item.get("shippingOptions") or []
    for o in opts:
        c = money(o.get("shippingCost"))
        if c is not None:
            return c, False
    return s["unknown_ship_in"], True


def evaluate(item, w, single_value, cfg, now, mode):
    s = cfg["settings"]
    title = item.get("title", "")
    tn = norm(title)
    if not title_ok(tn, w, cfg):
        return None
    if any_word(tn, ["for parts", "as is", "read description", "damaged", "stain", "stained", "holes"]):
        return None
    seller = item.get("seller") or {}
    try:
        if int(seller.get("feedbackScore", 0)) < s["min_seller_feedback"]:
            return None
        if float(seller.get("feedbackPercentage", 0)) < s["min_seller_percent"]:
            return None
    except (TypeError, ValueError):
        return None

    if mode == "auction":
        price = money(item.get("currentBidPrice")) or money(item.get("price"))
    else:
        price = money(item.get("price"))
        created = item.get("itemCreationDate")
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if now - dt > timedelta(hours=s["max_listing_age_hours"]):
                    return None
            except ValueError:
                pass
    if not price:
        return None

    n = 1
    if w.get("lot_only") and not is_lot(tn):
        return None
    if is_lot(tn):
        n = lot_count(title, s["max_lot_size"])
        if not n:
            return None
    ship_in, ship_guess = ship_cost(item, s)
    cost = price + ship_in
    unit_cost = cost / n

    if n == 1:
        resale = single_value
        per_piece = single_value
    else:
        per_piece = round(single_value * s["lot_piece_factor"], 2)
        resale = per_piece * n

    rate = s["fee_rate"] + s["promo_rate"]
    fees = (resale + s["buyer_shipping"] * n) * rate + s["fee_fixed"] * n
    profit = round(resale - fees - cost, 2)
    floor = s["min_profit"] if n > 1 else s.get("min_profit_single", s["min_profit"])
    if profit < floor:
        return None
    if unit_cost > single_value * s["max_cost_ratio"]:
        return None

    return {
        "id": item.get("itemId"), "title": title, "url": item.get("itemWebUrl"),
        "search": w["name"], "mode": mode, "price": price, "ship_in": ship_in,
        "ship_guess": ship_guess, "count": n, "per_piece": per_piece,
        "resale": round(resale, 2), "profit": profit,
        "condition": item.get("condition"),
        "ends": item.get("itemEndDate"),
    }


def message_for(d):
    head = f"${d['profit']:.0f} profit: {d['search']}"
    lines = []
    what = f"Lot of {d['count']}" if d["count"] > 1 else "Single item"
    if d["mode"] == "sgw":
        lines.append(f"{what}, ShopGoodwill auction, current bid ${d['price']:.2f}, {d['bids']} bids, {d['hours_left']}h left")
    elif d["mode"] == "auction":
        lines.append(f"{what}, auction, current bid ${d['price']:.2f}, ends {d['ends'][11:16] if d['ends'] else 'soon'} UTC")
    else:
        lines.append(f"{what}, Buy It Now ${d['price']:.2f}")
    lines.append(f"Shipping to you ${d['ship_in']:.2f}" + (" (guess)" if d["ship_guess"] else ""))
    if d["count"] > 1:
        lines.append(f"Est. resale ${d['per_piece']:.0f} each, ${d['resale']:.0f} total")
    else:
        lines.append(f"Est. resale ${d['resale']:.0f}")
    if d.get("condition"):
        lines.append(f"Condition: {d['condition']}")
    lines.append(d["title"])
    lines.append("Estimate is from current eBay asking prices. Check sold comps before buying.")
    return head, "\n".join(lines)


def notify(topic, d):
    title, body = message_for(d)
    payload = {"topic": topic, "title": title, "message": body, "click": d["url"],
               "tags": ["moneybag"], "priority": 4 if d["profit"] >= 50 else 3}
    try:
        r = requests.post(NTFY_URL, json=payload, timeout=20)
        if r.status_code >= 300:
            print(f"  ntfy failed ({r.status_code}): {r.text[:200]}")
    except requests.RequestException as e:
        print(f"  ntfy error: {e}")


# ---------- main ----------

def run():
    cfg = load_config()
    s = cfg["settings"]
    dry = os.environ.get("DRY_RUN") == "1"
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    token = get_token(os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"])
    now = datetime.now(timezone.utc)
    end_by = (now + timedelta(hours=s["auction_window_hours"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    do_auctions = (now.minute < 30 and now.hour % 2 == 0) or os.environ.get("FORCE_AUCTIONS") == "1"  # every 2 hours

    seen = load_json(SEEN_PATH, {})
    market = load_json(MARKET_PATH, {})
    deals = {}
    no_price = []

    for w in cfg["watch"]:
        value = market_price(token, w, cfg, market, now)
        if not value:
            no_price.append(w["name"])
            continue
        base = ["itemLocationCountry:US", "priceCurrency:USD"] + condition_filter(w["condition"])
        # The main search already includes lots, so one search per mode keeps
        # us well under eBay's daily limit.
        modes = [("bin", base + ["buyingOptions:{FIXED_PRICE}"], "newlyListed")]
        if do_auctions:
            modes.append(("auction", base + ["buyingOptions:{AUCTION}", f"itemEndDate:[..{end_by}]"], "endingSoonest"))
        for mode, filters, sort in modes:
                for item in search(token, w["query"], filters, sort, s["category_id"], limit=100):
                    iid = item.get("itemId")
                    if not iid or iid in seen or iid in deals:
                        continue
                    d = evaluate(item, w, value, cfg, now, mode)
                    if d:
                        deals[iid] = d

    # ShopGoodwill lots: same searches, valued with the same eBay prices
    if s.get("shopgoodwill", True):
        for w in cfg["watch"]:
            if not w.get("lot_only"):
                continue
            value = market_price(token, w, cfg, market, now)
            if not value:
                continue
            for item in search_sgw(w["query"]):
                d = evaluate_sgw(item, w, value, cfg, now)
                if d and d["id"] not in seen and d["id"] not in deals:
                    deals[d["id"]] = d

    ranked = sorted(deals.values(), key=lambda d: d["profit"], reverse=True)[: s["max_alerts_per_run"]]
    print(f"{CALLS['n']} eBay calls. {len(ranked)} new deals.")
    summary = ["## Deals this run", ""]
    for d in ranked:
        title, body = message_for(d)
        print(f"\n{title}\n{body}\n{d['url']}")
        summary.append(f"- **{title}** [{d['title']}]({d['url']})")
        if not dry and topic:
            notify(topic, d)
        seen[d["id"]] = now.timestamp()
    if not ranked:
        summary.append("No new deals.")
    if no_price:
        summary += ["", "Searches without enough listings to price: " + ", ".join(no_price)]

    cutoff = now.timestamp() - 14 * 86400
    seen = {k: v for k, v in seen.items() if v > cutoff}
    SEEN_PATH.write_text(json.dumps(seen, indent=0))
    MARKET_PATH.write_text(json.dumps(market, indent=1, sort_keys=True))
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a") as f:
            f.write("\n".join(summary) + "\n")
    return ranked


if __name__ == "__main__":
    run()
