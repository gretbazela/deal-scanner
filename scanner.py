"""Spyhopper deal scanner.

Searches eBay (official Browse API) for new Buy It Now listings and auctions
ending soon, values each listing with the price book in config.yaml, and sends
anything with enough profit to the ntfy app on your phone.

Environment variables (set as GitHub secrets):
  EBAY_CLIENT_ID      eBay App ID (Client ID), Production keyset
  EBAY_CLIENT_SECRET  eBay Cert ID (Client Secret), Production keyset
  NTFY_TOPIC          ntfy topic your phone subscribes to
Optional:
  DRY_RUN=1           print deals instead of sending alerts
  FORCE_AUCTIONS=1    check auctions on this run even if not on the hour
"""

import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.yaml"
SEEN_PATH = HERE / "seen.json"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
NTFY_URL = "https://ntfy.sh/"


# ---------- text matching ----------

def norm(text):
    """Lowercase, drop apostrophes, turn other punctuation into spaces."""
    text = text.lower().replace("'", "").replace("’", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " " + " ".join(text.split()) + " "


def has(title_n, term):
    return norm(term) in title_n


def load_config(path=CONFIG_PATH):
    """Read config.yaml and make sure every word list holds text."""
    cfg = yaml.safe_load(Path(path).read_text())
    for spec in cfg["brands"].values():
        for k in ("ok", "typo"):
            spec[k] = [str(t) for t in spec.get(k, []) or []]
    for rule in cfg["items"]:
        rule["brand"] = str(rule["brand"])
        rule["all"] = [[str(t) for t in g] for g in rule.get("all", []) or []]
        rule["none"] = [str(t) for t in rule.get("none", []) or []]
    return cfg


def build_brands(cfg):
    brands = {}
    for key, spec in cfg["brands"].items():
        brands[key] = {
            "ok": [t for t in spec.get("ok", [])],
            "typo": [t for t in spec.get("typo", [])],
        }
    return brands


def brand_hit(title_n, brand):
    """Return 'ok', 'typo', or None."""
    if any(has(title_n, t) for t in brand["ok"]):
        return "ok"
    if any(has(title_n, t) for t in brand["typo"]):
        return "typo"
    return None


LOT_PATTERNS = [
    r"\blot of (\d{1,3})\b",
    r"\b(\d{1,3}) ?(?:pairs?|pcs?|pieces?|items?)\b",
    r"\blot (?:of )?\(?(\d{1,3})\)?\b",
    r"\b(\d{1,3}) ?x\b",
]


def lot_count(title):
    t = title.lower()
    for pat in LOT_PATTERNS:
        m = re.search(pat, t)
        if m:
            n = int(m.group(1))
            if 2 <= n <= 200:
                return n
    return None


def value_listing(title, cfg, brands):
    """Find the price book rule for a title. Returns dict or None."""
    title_n = norm(title)
    best = None
    for rule in cfg["items"]:
        status = "ok"
        if rule["brand"] != "any":
            status = brand_hit(title_n, brands[rule["brand"]])
            if not status:
                continue
        if any(has(title_n, w) for w in rule.get("none", [])):
            continue
        if not all(any(has(title_n, w) for w in group) for group in rule.get("all", [])):
            continue
        if "per_piece" in rule:
            n = lot_count(title)
            if not n:
                continue
            resale, pieces = rule["per_piece"] * n, n
        else:
            resale, pieces = rule["resale"], 1
        cand = {"rule": rule["name"], "resale": resale, "pieces": pieces,
                "misspelled": status == "typo",
                "specificity": len(rule.get("all", []))}
        # prefer the most specific rule; on ties, the lower (safer) value
        if (best is None or cand["specificity"] > best["specificity"]
                or (cand["specificity"] == best["specificity"] and cand["resale"] < best["resale"])):
            best = cand
    return best


# ---------- profit ----------

def profit(resale, cost, ship_in, s):
    fees = s["fee_rate"] * (resale + s["buyer_shipping"]) + s["fee_fixed"]
    promo = s["promo_rate"] * resale
    return round(resale - cost - ship_in - fees - promo, 2)


def ship_cost(item, s):
    for opt in item.get("shippingOptions", []) or []:
        cost = (opt.get("shippingCost") or {}).get("value")
        if cost is not None:
            return float(cost), False
    return s["unknown_ship_in"], True


# ---------- eBay API ----------

def get_token(client_id, secret):
    auth = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    r = requests.post(
        TOKEN_URL,
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials",
              "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def search(token, q, filters, sort, category, limit=100):
    params = {"q": q, "filter": ",".join(filters), "limit": str(limit),
              "category_ids": category}
    if sort:
        params["sort"] = sort
    r = requests.get(
        SEARCH_URL,
        headers={"Authorization": f"Bearer {token}",
                 "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        params=params,
        timeout=30,
    )
    if r.status_code != 200:
        print(f"  search failed ({r.status_code}) for {q!r}: {r.text[:200]}")
        return []
    return r.json().get("itemSummaries", []) or []


def or_group(terms, cap=6):
    terms = list(dict.fromkeys(terms))[:cap]
    return terms[0] if len(terms) == 1 else "(" + ", ".join(terms) + ")"


def build_queries(cfg, brands):
    """One query per price book rule, plus one misspelling sweep per brand."""
    s = cfg["settings"]
    queries = []
    for rule in cfg["items"]:
        parts = []
        if rule["brand"] != "any":
            parts.append(brands[rule["brand"]]["ok"][0])
        groups = rule.get("all", [])
        if groups:
            parts.append(or_group(groups[0]))
        if "per_piece" in rule:
            cap = None
        else:
            cap = max(rule["resale"] - s["min_profit"], 5)
        queries.append({"q": " ".join(parts), "max_price": cap, "label": rule["name"]})
    for key, b in brands.items():
        if b["typo"]:
            queries.append({"q": or_group(b["typo"], cap=10), "max_price": 400,
                            "label": f"{key} misspellings"})
    return queries


# ---------- seen list ----------

def load_seen():
    if SEEN_PATH.exists():
        try:
            return json.loads(SEEN_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_seen(seen):
    cutoff = time.time() - 45 * 86400
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_PATH.write_text(json.dumps(seen, indent=0, sort_keys=True))


# ---------- evaluation ----------

def parse_time(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def evaluate(item, cfg, brands, now, mode):
    s = cfg["settings"]
    title = item.get("title", "")
    seller = item.get("seller") or {}
    try:
        if int(seller.get("feedbackScore", 0)) < s["min_seller_feedback"]:
            return None
        if float(seller.get("feedbackPercentage", 100)) < s["min_seller_percent"]:
            return None
    except (TypeError, ValueError):
        pass

    if mode == "bin":
        created = parse_time(item.get("itemCreationDate"))
        if created and now - created > timedelta(hours=s["max_listing_age_hours"]):
            return None
        price = float((item.get("price") or {}).get("value", 0) or 0)
    else:
        bid = item.get("currentBidPrice") or item.get("price") or {}
        price = float(bid.get("value", 0) or 0)
    if price <= 0:
        return None

    val = value_listing(title, cfg, brands)
    if not val:
        return None
    ship_in, ship_guess = ship_cost(item, s)
    p = profit(val["resale"], price, ship_in, s)
    if p < s["min_profit"]:
        return None
    return {
        "id": item.get("itemId"),
        "title": title,
        "url": item.get("itemWebUrl"),
        "mode": mode,
        "price": price,
        "ship_in": ship_in,
        "ship_guess": ship_guess,
        "resale": val["resale"],
        "rule": val["rule"],
        "pieces": val["pieces"],
        "misspelled": val["misspelled"],
        "profit": p,
        "bids": item.get("bidCount"),
        "ends": item.get("itemEndDate"),
        "condition": item.get("condition"),
    }


def pacific(ts):
    dt = parse_time(ts)
    if not dt:
        return ""
    # Pacific time without extra libraries: PST is UTC-8, PDT is UTC-7
    year = dt.year
    mar = datetime(year, 3, 8, 10, tzinfo=timezone.utc)
    dst_start = mar + timedelta(days=(6 - mar.weekday()) % 7)
    nov = datetime(year, 11, 1, 9, tzinfo=timezone.utc)
    dst_end = nov + timedelta(days=(6 - nov.weekday()) % 7)
    offset = 7 if dst_start <= dt < dst_end else 8
    local = dt - timedelta(hours=offset)
    return local.strftime("%a %-I:%M %p")


def message_for(d):
    lines = []
    head = f"${d['profit']:.0f} profit: {d['rule']}"
    if d["pieces"] > 1:
        head += f" ({d['pieces']} pieces)"
    if d["mode"] == "auction":
        lines.append(f"Auction, current bid ${d['price']:.2f} ({d['bids'] or 0} bids), ends {pacific(d['ends'])}")
        lines.append("Profit shown is at the current bid, so set your max accordingly.")
    else:
        lines.append(f"Buy It Now ${d['price']:.2f}")
    ship = f"${d['ship_in']:.2f}" + (" (guess, calculated shipping)" if d["ship_guess"] else "")
    lines.append(f"Shipping to you {ship}")
    lines.append(f"Typical eBay resale ${d['resale']:.0f}")
    if d["misspelled"]:
        lines.append("Misspelled brand in title, so most buyers miss it")
    if d.get("condition"):
        lines.append(f"Condition: {d['condition']}")
    lines.append(f"Title: {d['title']}")
    lines.append("Check photos, tags and seller before buying.")
    return head, "\n".join(lines)


def notify(topic, d):
    title, body = message_for(d)
    payload = {"topic": topic, "title": title, "message": body,
               "click": d["url"], "tags": ["moneybag"],
               "priority": 4 if d["profit"] >= 50 else 3}
    r = requests.post(NTFY_URL, json=payload, timeout=20)
    if r.status_code >= 300:
        print(f"  ntfy failed ({r.status_code}): {r.text[:200]}")


# ---------- main ----------

def run():
    cfg = load_config()
    s = cfg["settings"]
    brands = build_brands(cfg)
    dry = os.environ.get("DRY_RUN") == "1"
    topic = os.environ.get("NTFY_TOPIC", "")
    token = get_token(os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"])

    now = datetime.now(timezone.utc)
    do_auctions = now.minute < 20 or os.environ.get("FORCE_AUCTIONS") == "1"
    end_by = (now + timedelta(hours=s["auction_window_hours"])).strftime("%Y-%m-%dT%H:%M:%SZ")

    seen = load_seen()
    deals = {}
    queries = build_queries(cfg, brands)
    print(f"Running {len(queries)} searches (auctions: {'yes' if do_auctions else 'no'})")

    for qd in queries:
        base = ["itemLocationCountry:US", "deliveryCountry:US"]
        if qd["max_price"]:
            base.append(f"price:[..{qd['max_price']}]")
            base.append("priceCurrency:USD")
        modes = [("bin", base + ["buyingOptions:{FIXED_PRICE}"], "newlyListed")]
        if do_auctions:
            modes.append(("auction", base + ["buyingOptions:{AUCTION}", f"itemEndDate:[..{end_by}]"], "endingSoonest"))
        for mode, filters, sort in modes:
            for item in search(token, qd["q"], filters, sort, s["category_id"]):
                iid = item.get("itemId")
                if not iid or iid in seen or iid in deals:
                    continue
                d = evaluate(item, cfg, brands, now, mode)
                if d:
                    deals[iid] = d

    ranked = sorted(deals.values(), key=lambda d: d["profit"], reverse=True)
    print(f"Found {len(ranked)} new deals")
    summary = ["## Deals this run", ""]
    for d in ranked:
        title, body = message_for(d)
        print(f"\n{title}\n{body}\n{d['url']}")
        summary.append(f"- **{title}** [{d['title']}]({d['url']})")
        if not dry and topic:
            notify(topic, d)
        seen[d["id"]] = time.time()
    if not ranked:
        summary.append("No new deals.")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as f:
            f.write("\n".join(summary) + "\n")
    if not dry:
        save_seen(seen)
    return ranked


if __name__ == "__main__":
    try:
        run()
    except requests.HTTPError as e:
        print(f"eBay request failed: {e}")
        sys.exit(1)
