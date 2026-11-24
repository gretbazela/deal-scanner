"""Fast deal watcher.

Instead of checking every search once an hour, this stays running for about
five and a half hours and looks at the newest eBay listings every minute. Each
new listing is matched against your watch list in config.yaml and valued with
the same going prices as scanner.py. Deals go to ntfy within about a minute of
being listed. The workflow then starts itself again.

Environment: EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, NTFY_TOPIC
Optional: DRY_RUN=1 (print instead of send), FAST_ONCE=1 (one pass, then stop)
"""

import copy
import json
import os
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import scanner as sc

RUN_SECONDS = int(os.environ.get("FAST_RUN_SECONDS", 5 * 3600 + 30 * 60))
CALL_BUDGET = int(os.environ.get("FAST_CALL_BUDGET", 1100))  # eBay calls per run
PACIFIC = ZoneInfo("America/Los_Angeles")

# Broad searches over the newest listings, with how often to run each one
# (seconds). Matching to your brands happens here, not in eBay's search, so
# one call covers many watches.
FEEDS = [
    ("shorts", 60),
    ("board shorts", 60),
    ("shorts lot", 120),
    ("jorts", 300),
    ("trunks", 300),
]
STATS = {"scanned": 0, "matched": 0, "money_blocked": 0, "other_blocked": 0, "near_miss_best": -9999.0}
RELAXED = {}
WATCHLIST = {}  # near misses to re-check for price drops: id -> dict
WATCH_MAX = 60
GONE = {"n": 0, "fast": 0}  # listings that vanished, and how many within 6 hours


def relaxed_cfg(cfg):
    """Same settings with the money rules switched off, to see what blocked a listing."""
    if "cfg" not in RELAXED:
        c = copy.deepcopy(cfg)
        c["settings"].update({"min_profit": -9999, "min_profit_single": -9999, "max_cost_ratio": 999})
        RELAXED["cfg"] = c
    return RELAXED["cfg"]
SGW_EVERY = 300

# Common misspellings of your brands. Sellers who misspell a brand get fewer
# views, so these listings sit longer and are often cheap.
TYPOS = {
    "hurly": "hurley", "hurlley": "hurley", "hurely": "hurley", "hurly phantom": "hurley phantom",
    "quicksilver": "quiksilver", "quiksliver": "quiksilver", "quicksliver": "quiksilver", "quiksilvr": "quiksilver",
    "billabon": "billabong", "bilabong": "billabong", "billibong": "billabong", "billabang": "billabong",
    "patagoina": "patagonia", "patagonai": "patagonia", "patogonia": "patagonia", "pategonia": "patagonia", "patagonia's": "patagonia",
    "carhart": "carhartt", "carharrt": "carhartt", "carhartts": "carhartt", "carhatt": "carhartt",
    "oneil": "oneill", "o neil": "oneill", "o neill": "oneill",
    "volcome": "volcom", "volcomb": "volcom",
    "stusy": "stussy", "stussi": "stussy", "stuessy": "stussy",
    "lulu lemon": "lululemon", "lululmon": "lululemon", "lulelemon": "lululemon", "lulemon": "lululemon",
    "arc teryx": "arcteryx", "arcterx": "arcteryx",
    "vouri": "vuori", "vuoori": "vuori",
    "peter miller": "peter millar", "peter millars": "peter millar",
    "travis matthew": "travis mathew", "travismathew": "travis mathew",
    "vinyard vines": "vineyard vines", "vineyards vines": "vineyard vines",
    "johnny o": "johnnie o",
    "birdwel": "birdwell", "bird well": "birdwell",
    "visla": "vissla", "vissala": "vissla",
    "ripcurl": "rip curl", "rip curls": "rip curl",
    "bordshorts": "board shorts", "boardshort": "board shorts", "boardshorts": "board shorts", "board short": "board shorts",
}
TYPO_RES = [(re.compile(r"\b" + re.escape(w) + r"\b", re.I), r) for w, r in TYPOS.items()]


def fix_title(title):
    for rx, right in TYPO_RES:
        title = rx.sub(right, title)
    return title


def try_offer(item, w, value, cfg, now):
    """For Best Offer listings: would it be a deal if the seller took a lower offer?"""
    price = sc.money(item.get("price"))
    if not price:
        return None
    ratio = cfg["settings"].get("offer_ratio", 0.75)
    offer = round(price * ratio, 2)
    cheaper = dict(item, price={"value": str(offer), "currency": "USD"})
    d = sc.evaluate(cheaper, w, value, cfg, now, "bin")
    if d:
        d["offer_from"] = price
    return d
AUCTION_EVERY = 300
NIGHT_SLOWDOWN = 5  # midnight to 5 AM Pacific, everything runs this many times slower


def cond_ok(item, w):
    c = (item.get("condition") or "").lower()
    if w["condition"] == "new":
        return c.startswith("new")
    if w["condition"] == "used":
        return not c.startswith("new")
    return True


def feed_items(token, q, s, auctions_soon):
    base = ["itemLocationCountry:US", "priceCurrency:USD"]
    if auctions_soon:
        end_by = datetime.fromtimestamp(time.time() + s["auction_window_hours"] * 3600, timezone.utc)
        filters = base + ["buyingOptions:{AUCTION}", "itemEndDate:[.." + end_by.strftime("%Y-%m-%dT%H:%M:%SZ") + "]"]
        return sc.search(token, q, filters, "endingSoonest", s["category_id"], limit=200)
    filters = base + ["buyingOptions:{FIXED_PRICE|AUCTION}"]
    return sc.search(token, q, filters, "newlyListed", s["category_id"], limit=200)


def evaluate_items(items, watches, cfg, now, processed, seen):
    """Match listings to watches. Returns the best deal per listing."""
    found = {}
    for item in items:
        iid = item.get("itemId")
        if not iid or iid in seen:
            continue
        mode = "auction" if "AUCTION" in (item.get("buyingOptions") or []) else "bin"
        key = (iid, mode, (item.get("currentBidPrice") or {}).get("value") if mode == "auction" else (item.get("price") or {}).get("value"))
        if key in processed:
            continue
        processed.add(key)
        STATS["scanned"] += 1
        title = item.get("title") or ""
        fixed = fix_title(title)
        typo = fixed != title
        view = dict(item, title=fixed) if typo else item
        tn = sc.norm(fixed)
        best = None
        hit = False
        money = []
        near = None
        for w, value in watches:
            if not cond_ok(item, w):
                continue
            if not sc.title_ok(tn, w, cfg):
                continue
            hit = True
            d = sc.evaluate(view, w, value, cfg, now, mode)
            if d is None and mode == "bin" and "BEST_OFFER" in (item.get("buyingOptions") or []):
                d = try_offer(view, w, value, cfg, now)
            if d is None:
                d2 = sc.evaluate(view, w, value, relaxed_cfg(cfg), now, mode)
                if d2:
                    money.append(d2["profit"])
                    if mode == "bin" and d2["profit"] > -10 and (near is None or d2["profit"] > near[0]):
                        near = (d2["profit"], view, w, value)
            if d and (best is None or d["profit"] > best["profit"]):
                best = d
        if hit:
            STATS["matched"] += 1
            if best is None:
                if near and len(WATCHLIST) < WATCH_MAX:
                    WATCHLIST[iid] = {"item": near[1], "w": near[2], "value": near[3],
                                      "price": sc.money(near[1].get("price")), "at": time.time(), "typo": typo}
                if money:
                    STATS["money_blocked"] += 1
                    STATS["near_miss_best"] = max(STATS["near_miss_best"], max(money))
                else:
                    STATS["other_blocked"] += 1
        if best:
            if typo:
                best["title"] = title + " [misspelled title]"
            found[iid] = best
    return found


def recheck_watchlist(token, cfg, now, seen):
    """Look again at near misses. If the seller dropped the price into deal range, alert."""
    found = {}
    for iid in list(WATCHLIST)[:12]:
        e = WATCHLIST[iid]
        if time.time() - e["at"] > 48 * 3600:
            del WATCHLIST[iid]
            continue
        cur = sc.get_item(token, iid)
        if cur == "error":
            continue
        if cur is None:
            GONE["n"] += 1
            if time.time() - e["at"] < 6 * 3600:
                GONE["fast"] += 1
            del WATCHLIST[iid]
            continue
        price = sc.money(cur.get("price"))
        if not price or not e["price"] or price >= e["price"]:
            continue
        item = dict(e["item"], price=cur.get("price"))
        d = sc.evaluate(item, e["w"], e["value"], cfg, now, "bin")
        if d is None and "BEST_OFFER" in (item.get("buyingOptions") or []):
            d = try_offer(item, e["w"], e["value"], cfg, now)
        if d and iid not in seen:
            d["title"] = d["title"] + f" [price dropped from ${e['price']:.2f}]"
            found[iid] = d
            del WATCHLIST[iid]
        else:
            e["price"] = price
    return found


def send(d, topic, dry, seen, now):
    title, body = sc.message_for(d)
    if dry:
        print(f"\n{title}\n{body}\n{d['url']}")
    elif topic:
        sc.notify(topic, d)
    seen[d["id"]] = now.timestamp()


def save(seen, market):
    cutoff = time.time() - 14 * 86400
    seen = {k: v for k, v in seen.items() if v > cutoff}
    sc.SEEN_PATH.write_text(json.dumps(seen, indent=0))
    sc.MARKET_PATH.write_text(json.dumps(market, indent=1, sort_keys=True))
    return seen


def main():
    cfg = sc.load_config()
    s = cfg["settings"]
    dry = os.environ.get("DRY_RUN") == "1"
    once = os.environ.get("FAST_ONCE") == "1"
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    token = sc.get_token(os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"])
    started = time.time()
    seen = sc.load_json(sc.SEEN_PATH, {})
    market = sc.load_json(sc.MARKET_PATH, {})
    processed = set()
    due = {}
    alerts = 0
    cycles = 0
    last_value_refresh = 0.0
    watches = []
    sgw_watches = []

    while True:
        now = datetime.now(timezone.utc)
        if not once and time.time() - started > RUN_SECONDS:
            break

        # Going prices, refreshed every few hours (each is cached a day in market.json)
        if time.time() - last_value_refresh > 3 * 3600:
            watches, sgw_watches = [], []
            for w in cfg["watch"]:
                value = sc.market_price(token, w, cfg, market, now)
                if value:
                    watches.append((w, value))
                    if w.get("lot_only"):
                        sgw_watches.append((w, value))
            last_value_refresh = time.time()
            save(seen, market)
            print(f"{len(watches)} searches priced, {sc.CALLS['n']} eBay calls so far", flush=True)

        slow = NIGHT_SLOWDOWN if datetime.now(PACIFIC).hour < 5 else 1
        if sc.CALLS["n"] > CALL_BUDGET:
            slow = max(slow, 4)
        found = {}

        for q, every in FEEDS:
            k = ("feed", q)
            if once or time.time() >= due.get(k, 0):
                due[k] = time.time() + every * slow
                found.update(evaluate_items(feed_items(token, q, s, False), watches, cfg, now, processed, seen))

        k = ("auctions",)
        if once or time.time() >= due.get(k, 0):
            due[k] = time.time() + AUCTION_EVERY * slow
            for q in ("shorts", "board shorts"):
                found.update(evaluate_items(feed_items(token, q, s, True), watches, cfg, now, processed, seen))

        k = ("sgw",)
        if s.get("shopgoodwill", True) and (once or time.time() >= due.get(k, 0)):
            due[k] = time.time() + SGW_EVERY * slow
            for w, value in sgw_watches:
                for item in sc.search_sgw(w["query"]):
                    d = sc.evaluate_sgw(item, w, value, cfg, now)
                    if d and d["id"] not in seen and d["id"] not in found:
                        found[d["id"]] = d

        k = ("recheck",)
        if once or time.time() >= due.get(k, 0):
            due[k] = time.time() + 600 * slow
            found.update(recheck_watchlist(token, cfg, now, seen))

        ranked = sorted(found.values(), key=lambda d: d["profit"], reverse=True)[:10]
        for d in ranked:
            if d["id"] in seen:
                continue
            send(d, topic, dry, seen, now)
            alerts += 1

        cycles += 1
        if len(processed) > 50000:
            processed.clear()
        if cycles % 30 == 0:
            seen = save(seen, market)
            print(f"{cycles} passes, {alerts} alerts, {sc.CALLS['n']} eBay calls, {STATS['scanned']} listings checked, {STATS['matched']} matched a watch, {STATS['money_blocked']} blocked by profit rules, {STATS['other_blocked']} blocked by other rules, best blocked profit ${STATS['near_miss_best']:.0f}, {len(WATCHLIST)} near misses watched, {GONE['n']} gone ({GONE['fast']} within 6h)", flush=True)
        if once:
            break
        time.sleep(15)

    save(seen, market)
    print(f"done: {cycles} passes, {alerts} alerts, {sc.CALLS['n']} eBay calls")


if __name__ == "__main__":
    main()
