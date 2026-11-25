"""Spyhopper deal hunter, version 3.

Works from a buy list (buylist.yaml) of items with known SOLD prices, not
from asking prices. For every item it works out the most you can pay and
still make the target profit, then asks eBay only for listings at or under
that price. Anything that clears the bar goes to ntfy on your phone with
Bought / Pass / Wrong buttons, and every alert is logged to alerts.csv.

Searches per item:
  bin      newest Buy It Now listings under the max price
  auction  auctions ending soon with the current bid under the max bid
  typo     the same, searched under common misspellings
  lot      lots of the item, valued per piece

Environment: EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, NTFY_TOPIC
Optional: DRY_RUN=1 (print instead of send), HUNT_ONCE=1 (one pass),
          HUNT_RUN_SECONDS (default 5.5 hours)
"""

import base64
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
import yaml

HERE = Path(__file__).parent
BUYLIST_PATH = HERE / "buylist.yaml"
SEEN_PATH = HERE / "seen.json"
STATE_PATH = HERE / "state.json"
ALERTS_PATH = HERE / "alerts.csv"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
NTFY = "https://ntfy.sh/"
TOKEN_MAX_AGE = 90 * 60  # eBay tokens last 2 hours; refresh well before
LOT_WORDS = ["lot", "bundle", "wholesale", "bulk"]
ALERT_FIELDS = ["sent_at", "item", "kind", "listing_id", "url", "title", "condition",
                "price", "ship_in", "landed", "resale_est", "profit_est", "max_price",
                "count", "outcome", "outcome_at", "resale_actual", "notes"]
CALLS = {"n": 0}


# ---------- text helpers ----------

def norm(text):
    text = (text or "").lower().replace("'", "").replace("’", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " " + " ".join(text.split()) + " "


def has(title_n, term):
    return norm(term) in title_n


def any_word(title_n, words):
    return any(has(title_n, w) for w in words or [])


LOOKALIKE = re.compile(r" like (?!new )| vibes? | aesthetic | esque | comparable | similar ")

LOT_PATTERNS = [
    r"\blot of (\d{1,3})\b",
    r"\b(\d{1,3}) ?(?:pairs?|pcs?|pieces?|items?|jackets|shirts|pants|shorts|sweaters)\b",
    r"\blot (?:of )?\(?(\d{1,3})\)?\b",
    r"\b(\d{1,3}) ?x\b",
    r"\bset of (\d{1,3})\b",
]


def lot_count(title, max_lot):
    """Pieces in a lot, or None if the title doesn't say clearly."""
    t = title.lower()
    t = re.sub(r"\bsizes?:? ?\d{1,2}(?: ?(?:&|and|/|,) ?\d{1,2})*\b", " ", t)
    t = re.sub(r"\b\d{2} ?x ?\d{2}\b", " ", t)
    t = re.sub(r"\b[wl]\d{2}\b", " ", t)
    found = set()
    for pat in LOT_PATTERNS:
        for m in re.finditer(pat, t):
            n = int(m.group(1))
            if 2 <= n <= max_lot:
                found.add(n)
    return found.pop() if len(found) == 1 else None


def money(obj):
    try:
        return float(obj["value"])
    except (TypeError, KeyError, ValueError):
        return None


# ---------- buy list and the money math ----------

# Money settings an item or a group may change for itself (see "costs:" in buylist.yaml).
COST_KEYS = {"target_profit", "fee_rate", "promo_rate", "fee_fixed", "buyer_shipping", "postage_out",
             "buy_tax_rate", "unknown_ship_in", "typical_ship_in", "lot_piece_factor", "max_lot_size",
             "lot_buyer_shipping", "lot_postage_out"}


def costs_for(it, groups, base):
    """The settings one item uses: global settings, then its group's costs, then its own costs."""
    merged = dict(base)
    layers = []
    if it.get("group"):
        if it["group"] not in groups:
            raise ValueError(f"{it['name']}: group '{it['group']}' is not defined under groups:")
        layers.append((f"group {it['group']}", groups[it["group"]].get("costs") or {}))
    layers.append((it["name"], it.get("costs") or {}))
    for label, costs in layers:
        bad = set(costs) - COST_KEYS
        if bad:
            raise ValueError(f"{label}: unknown cost setting {sorted(bad)}. Allowed: {sorted(COST_KEYS)}")
        merged.update({k: float(v) for k, v in costs.items() if v not in (None, "")})
    return merged


def load_buylist(path=BUYLIST_PATH):
    cfg = yaml.safe_load(Path(path).read_text())
    cfg["groups"] = cfg.get("groups") or {}
    cfg["settings"].setdefault("typical_ship_in", 8.0)
    cfg["exclude_words"] = [str(w) for w in cfg.get("exclude_words") or []]
    cfg["damage_words"] = [str(w) for w in cfg.get("damage_words") or []]
    items = []
    for it in cfg.get("items") or []:
        it["query"] = str(it["query"])
        it.setdefault("name", it["query"])
        it["must"] = [[str(t) for t in (g if isinstance(g, list) else [g])] for g in (it.get("must") or [])]
        it["skip"] = [str(t) for t in (it.get("skip") or [])]
        it["typos"] = [str(t) for t in (it.get("typos") or [])]
        sold = it.get("sold") or {}
        it["sold"] = {k: float(v) for k, v in sold.items() if k in ("used", "new") and v not in (None, "", 0)}
        it["s"] = costs_for(it, cfg["groups"], cfg["settings"])
        items.append(it)
    cfg["items"] = items
    return cfg


def net_per_piece(resale, s):
    """What one sale puts in your pocket before you pay for the item."""
    rate = s["fee_rate"] + s["promo_rate"]
    gross = resale + s["buyer_shipping"]
    return gross - rate * gross - s["fee_fixed"] - s["postage_out"]


def net_total(resale, count, s):
    """What a sale of `count` pieces puts in your pocket before you pay for the items.

    Single pieces, and lots when no lot shipping is set, pay shipping and the
    per order fee on every piece (the cautious default). If lot_buyer_shipping and
    lot_postage_out are both set, a lot is treated as one parcel: one shipping
    charge, one label, one per order fee.
    """
    if count > 1 and s.get("lot_buyer_shipping") is not None and s.get("lot_postage_out") is not None:
        gross = resale * count + s["lot_buyer_shipping"]
        return gross - (s["fee_rate"] + s["promo_rate"]) * gross - s["fee_fixed"] - s["lot_postage_out"]
    return net_per_piece(resale, s) * count


def landed(price, ship_in, s):
    return (price + ship_in) * (1 + s["buy_tax_rate"])


def profit(resale, price, ship_in, s, count=1):
    return net_total(resale, count, s) - landed(price, ship_in, s)


def max_price(resale, ship_in, s, count=1):
    """Highest item price (before shipping and tax) that still clears the target."""
    room = net_total(resale, count, s) - s["target_profit"]
    return room / (1 + s["buy_tax_rate"]) - ship_in


def condition_of(item):
    c = (item.get("condition") or "").lower()
    if c.startswith("new") and "defect" not in c:
        return "new"
    return "used"


def title_ok(title_n, it, cfg):
    if any_word(title_n, cfg["exclude_words"]) or any_word(title_n, it["skip"]):
        return False
    if any_word(title_n, cfg["damage_words"]):
        return False
    if LOOKALIKE.search(title_n):
        return False
    squashed = title_n.replace(" ", "")
    for word in norm(it["query"]).split():
        if f" {word} " not in title_n and not (len(word) > 3 and word in squashed):
            return False
    return all(any_word(title_n, g) for g in it["must"])


def fix_typos(title_n, it):
    for t in it["typos"]:
        title_n = title_n.replace(norm(t), norm(it["query"]))
    return title_n


def is_lot(title_n):
    return any_word(title_n, LOT_WORDS)


# ---------- evaluating one listing ----------

def ship_in_of(listing, s):
    for o in listing.get("shippingOptions") or []:
        c = money(o.get("shippingCost"))
        if c is not None:
            return c, False
    return s["unknown_ship_in"], True


def seller_ok(listing, s):
    seller = listing.get("seller") or {}
    try:
        return (int(seller.get("feedbackScore", 0)) >= s["min_seller_feedback"]
                and float(seller.get("feedbackPercentage", 0)) >= s["min_seller_percent"])
    except (TypeError, ValueError):
        return False


def evaluate(listing, it, kind, cfg, now):
    """Return a deal dict, or None. kind is bin, auction, typo or lot."""
    s = it["s"]
    title = listing.get("title") or ""
    tn = norm(title)
    if kind == "typo":
        tn = fix_typos(tn, it)
    if not title_ok(tn, it, cfg) or not seller_ok(listing, s):
        return None
    cond = condition_of(listing)
    resale = it["sold"].get(cond)
    if not resale:
        return None

    lot = is_lot(tn)
    if kind == "lot" and not lot:
        return None
    count = 1
    if lot:
        count = lot_count(title, s["max_lot_size"])
        if not count:
            return None
        resale = round(resale * s["lot_piece_factor"], 2)

    auction = "AUCTION" in (listing.get("buyingOptions") or []) and "FIXED_PRICE" not in (listing.get("buyingOptions") or [])
    if auction:
        price = money(listing.get("currentBidPrice")) or money(listing.get("price"))
        end = listing.get("itemEndDate")
        if not end:
            return None
        hours_left = (datetime.fromisoformat(end.replace("Z", "+00:00")) - now).total_seconds() / 3600
        if hours_left < 0 or hours_left > s["auction_hours"]:
            return None
    else:
        price = money(listing.get("price"))
        created = listing.get("itemCreationDate")
        if created:
            age = now - datetime.fromisoformat(created.replace("Z", "+00:00"))
            if age > timedelta(hours=s["max_listing_age_hours"]):
                return None
    if not price:
        return None

    ship_in, ship_guess = ship_in_of(listing, s)
    cap = max_price(resale, ship_in, s, count)
    if price > cap:
        return None
    # For auctions the alert is "bid up to cap": profit is what you make at that bid.
    est = profit(resale, cap if auction else price, ship_in, s, count)
    return {
        "id": listing.get("itemId"), "url": listing.get("itemWebUrl"), "title": title,
        "item": it["name"], "kind": ("auction" if auction else "bin") + ("-lot" if count > 1 else "")
        + ("-typo" if kind == "typo" else ""),
        "condition": listing.get("condition") or cond, "price": round(price, 2),
        "ship_in": round(ship_in, 2), "ship_guess": ship_guess,
        "landed": round(landed(price, ship_in, s), 2), "count": count,
        "resale": round(resale * count, 2), "per_piece": resale,
        "profit": round(est, 2), "profit_now": round(profit(resale, price, ship_in, s, count), 2),
        "max_price": round(cap, 2), "auction": auction, "ends": listing.get("itemEndDate"),
        "best_offer": "BEST_OFFER" in (listing.get("buyingOptions") or []),
    }


# ---------- eBay ----------

class Ebay:
    def __init__(self, client_id, client_secret):
        self.id = "".join(client_id.split())
        self.secret = "".join(client_secret.split())
        self.token = None
        self.got_at = 0.0

    def _login(self):
        auth = base64.b64encode(f"{self.id}:{self.secret}".encode()).decode()
        r = requests.post(TOKEN_URL, timeout=30, headers={
            "Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"})
        if r.status_code != 200:
            print(f"eBay login failed {r.status_code}: {r.text[:300]}", flush=True)
            sys.exit(1)
        self.token = r.json()["access_token"]
        self.got_at = time.time()

    def _get(self, url, params=None):
        if not self.token or time.time() - self.got_at > TOKEN_MAX_AGE:
            self._login()
        for attempt in range(3):
            CALLS["n"] += 1
            try:
                r = requests.get(url, params=params, timeout=30, headers={
                    "Authorization": "Bearer " + self.token, "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
            except requests.RequestException as e:
                print(f"  eBay error: {e}", flush=True)
                time.sleep(3)
                continue
            if r.status_code == 401:
                self._login()
                continue
            if r.status_code in (429, 500, 502, 503):
                time.sleep(2 + attempt * 5)
                continue
            return r
        return None

    def search(self, q, filters, sort, limit=100):
        params = {"q": q, "limit": str(limit), "filter": ",".join(filters), "sort": sort}
        r = self._get(SEARCH_URL, params)
        if r is None or r.status_code != 200:
            code = r.status_code if r is not None else "no response"
            body = r.text[:200] if r is not None else ""
            print(f"  search failed ({code}) for '{q}': {body}", flush=True)
            return []
        return r.json().get("itemSummaries") or []


def build_searches(cfg):
    """One entry per eBay call we make on a schedule: (key, item, kind, query, every_n_rounds)."""
    s = cfg["settings"]
    out = []
    for it in cfg["items"]:
        if not it["sold"]:
            continue
        out.append((f"{it['name']}|bin", it, "bin", it["query"], 1))
        out.append((f"{it['name']}|auction", it, "auction", it["query"], 3))
        for t in it["typos"]:
            out.append((f"{it['name']}|typo|{t}", it, "typo", t, 6))
        if it.get("lots"):
            out.append((f"{it['name']}|lot", it, "lot", it["query"] + " lot", 6))
    return out


def filters_for(it, kind, cfg, now):
    s = it["s"]
    top = max(it["sold"].values())
    if kind == "lot":
        cap = max_price(top * s["lot_piece_factor"], 0, s, s["max_lot_size"])
    else:
        cap = max_price(top, 0, s)  # before shipping; evaluate() rechecks with real shipping
    f = ["itemLocationCountry:US", "priceCurrency:USD", f"price:[..{max(cap, 1):.2f}]"]
    if len(it["sold"]) == 1:
        f.append("conditions:{NEW}" if "new" in it["sold"] else "conditions:{USED}")
    if kind == "auction":
        end_by = (now + timedelta(hours=s["auction_hours"])).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f + ["buyingOptions:{AUCTION}", f"itemEndDate:[..{end_by}]"], "endingSoonest"
    if kind == "lot":
        return f + ["buyingOptions:{FIXED_PRICE|AUCTION}"], "newlyListed"
    return f + ["buyingOptions:{FIXED_PRICE}"], "newlyListed"


def round_seconds(searches, cfg):
    """How long one round should take so a day of rounds fits the call budget."""
    per_round = sum(1.0 / every for *_, every in searches)
    per_hour = cfg["settings"]["daily_call_budget"] / 24.0
    return max(60.0, per_round * 3600.0 / per_hour)


# ---------- alerts, feedback, log ----------

def message_for(d):
    head = f"${d['profit']:.0f} profit: {d['item']}"
    what = f"Lot of {d['count']}" if d["count"] > 1 else "Single"
    lines = []
    if d["auction"]:
        lines.append(f"{what}, AUCTION, bid now ${d['price']:.2f}, ends {(d['ends'] or '')[11:16]} UTC")
        lines.append(f"Bid up to ${d['max_price']:.2f} to make ${d['profit']:.0f}")
    else:
        lines.append(f"{what}, Buy It Now ${d['price']:.2f}" + (" (takes offers)" if d["best_offer"] else ""))
    lines.append(f"Shipping to you ${d['ship_in']:.2f}" + (" (guess)" if d["ship_guess"] else "")
                 + f", landed with tax ${d['landed']:.2f}")
    lines.append(f"Resale ${d['per_piece']:.0f}" + (f" each, ${d['resale']:.0f} total" if d["count"] > 1 else "")
                 + " (from your Terapeak sold price)")
    if "typo" in d["kind"]:
        lines.append("Found under a misspelled title")
    lines.append(f"Condition: {d['condition']}")
    lines.append(d["title"])
    return head, "\n".join(lines)


def comps_url(d):
    words = re.findall(r"[A-Za-z0-9']+", re.sub(r"\[.*?\]", " ", d["title"]))[:8]
    return "https://www.ebay.com/sch/i.html?_nkw=" + quote(" ".join(words)) + "&LH_Sold=1&LH_Complete=1"


def notify(topic, d):
    title, body = message_for(d)
    fb = NTFY + topic + "-fb"
    payload = {
        "topic": topic, "title": title, "message": body, "click": d["url"],
        "tags": ["rotating_light" if d["profit"] >= 50 else "moneybag"],
        "priority": 5 if d["profit"] >= 50 else 4,
        "actions": [
            {"action": "http", "label": "Bought", "url": fb, "body": f"bought {d['id']}", "clear": True},
            {"action": "http", "label": "Wrong", "url": fb, "body": f"wrong {d['id']}", "clear": True},
            {"action": "view", "label": "Sold comps", "url": comps_url(d)},
        ],
    }
    try:
        r = requests.post(NTFY, json=payload, timeout=20)
        if r.status_code >= 300:
            print(f"  ntfy failed ({r.status_code}): {r.text[:200]}", flush=True)
    except requests.RequestException as e:
        print(f"  ntfy error: {e}", flush=True)


def read_alerts():
    if not ALERTS_PATH.exists():
        return []
    with ALERTS_PATH.open(newline="") as f:
        return list(csv.DictReader(f))


def write_alerts(rows):
    with ALERTS_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ALERT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def log_alert(d, now):
    rows = read_alerts()
    rows.append({"sent_at": now.strftime("%Y-%m-%d %H:%M"), "item": d["item"], "kind": d["kind"],
                 "listing_id": d["id"], "url": d["url"], "title": d["title"], "condition": d["condition"],
                 "price": d["price"], "ship_in": d["ship_in"], "landed": d["landed"],
                 "resale_est": d["resale"], "profit_est": d["profit"], "max_price": d["max_price"],
                 "count": d["count"]})
    write_alerts(rows)


def apply_feedback(messages, rows):
    """Record Bought / Wrong taps. messages are ntfy JSON events. Returns how many applied."""
    by_id = {r["listing_id"]: r for r in rows}
    n = 0
    for m in messages:
        if m.get("event") != "message":
            continue
        parts = (m.get("message") or "").strip().split(None, 1)
        if len(parts) != 2 or parts[0] not in ("bought", "wrong", "pass"):
            continue
        row = by_id.get(parts[1])
        if row is None:
            continue
        row["outcome"] = parts[0]
        row["outcome_at"] = datetime.fromtimestamp(m.get("time", time.time()), timezone.utc).strftime("%Y-%m-%d %H:%M")
        n += 1
    return n


def poll_feedback(topic, state):
    since = state.get("fb_since", "24h")
    try:
        r = requests.get(f"{NTFY}{topic}-fb/json", params={"poll": "1", "since": since}, timeout=20)
    except requests.RequestException:
        return
    if r.status_code != 200:
        return
    msgs = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    if not msgs:
        return
    rows = read_alerts()
    if apply_feedback(msgs, rows):
        write_alerts(rows)
    state["fb_since"] = msgs[-1].get("id", since)


# ---------- main loop ----------

def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save(seen, state):
    cutoff = time.time() - 21 * 86400
    seen = {k: v for k, v in seen.items() if v > cutoff}
    SEEN_PATH.write_text(json.dumps(seen, indent=0, sort_keys=True))
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True))
    return seen


def main():
    cfg = load_buylist()
    searches = build_searches(cfg)
    if not searches:
        print("No items have a sold price yet. Fill in sold: in buylist.yaml (see README).")
        out = os.environ.get("GITHUB_OUTPUT")
        if out:  # don't restart in a loop; the 2-hourly schedule checks again
            with open(out, "a") as f:
                f.write("restart=false\n")
        return
    dry = os.environ.get("DRY_RUN") == "1"
    once = os.environ.get("HUNT_ONCE") == "1"
    run_seconds = int(os.environ.get("HUNT_RUN_SECONDS", 5 * 3600 + 30 * 60))
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    ebay = Ebay(os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"])
    seen = load_json(SEEN_PATH, {})
    state = load_json(STATE_PATH, {})
    gap = round_seconds(searches, cfg)
    active = len({it["name"] for _, it, *_ in searches})
    print(f"{active} items active, {len(searches)} searches, one round every {gap:.0f}s", flush=True)

    started = time.time()
    rnd = 0
    alerts = 0
    while once or time.time() - started < run_seconds:
        t0 = time.time()
        now = datetime.now(timezone.utc)
        found = {}
        for key, it, kind, q, every in searches:
            if rnd % every:
                continue
            filters, sort = filters_for(it, kind, cfg, now)
            for listing in ebay.search(q, filters, sort):
                iid = listing.get("itemId")
                if not iid or iid in seen:
                    continue
                d = evaluate(listing, it, kind, cfg, now)
                if d and (iid not in found or d["profit"] > found[iid]["profit"]):
                    found[iid] = d
        for d in sorted(found.values(), key=lambda x: x["profit"], reverse=True):
            title, body = message_for(d)
            if dry:
                print(f"\n{title}\n{body}\n{d['url']}", flush=True)
            else:
                if topic:
                    notify(topic, d)
                log_alert(d, now)
            seen[d["id"]] = now.timestamp()
            alerts += 1
        if topic and not dry:
            poll_feedback(topic, state)
        rnd += 1
        if rnd % 10 == 0:
            seen = save(seen, state)
            print(f"round {rnd}: {alerts} alerts, {CALLS['n']} eBay calls", flush=True)
        if once:
            break
        time.sleep(max(0.0, gap - (time.time() - t0)))

    save(seen, state)
    print(f"done: {rnd} rounds, {alerts} alerts, {CALLS['n']} eBay calls", flush=True)


if __name__ == "__main__":
    main()
