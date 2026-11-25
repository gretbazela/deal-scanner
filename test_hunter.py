"""Offline checks for hunter.py. Run: python test_hunter.py"""

import copy
from datetime import datetime, timedelta, timezone

import hunter as h

NOW = datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc)
CFG = h.load_buylist()
S = CFG["settings"]


def item(name, sold):
    it = copy.deepcopy(next(i for i in CFG["items"] if i["name"] == name))
    it["sold"] = sold
    return it


def listing(title, price, ship=8.0, cond="Pre-owned", opts=("FIXED_PRICE",), age_h=2, bid=None, ends_h=None, fb=200):
    d = {"itemId": "v1|1|0", "itemWebUrl": "https://ebay.com/itm/1", "title": title,
         "price": {"value": str(price)}, "condition": cond, "buyingOptions": list(opts),
         "seller": {"feedbackScore": fb, "feedbackPercentage": "99.5"},
         "shippingOptions": [{"shippingCost": {"value": str(ship)}}] if ship is not None else [],
         "itemCreationDate": (NOW - timedelta(hours=age_h)).isoformat().replace("+00:00", "Z")}
    if bid is not None:
        d["currentBidPrice"] = {"value": str(bid)}
    if ends_h is not None:
        d["itemEndDate"] = (NOW + timedelta(hours=ends_h)).isoformat().replace("+00:00", "Z")
    return d


def test_math_round_trips():
    for r in (60, 100, 180):
        cap = h.max_price(r, 8, S)
        assert abs(h.profit(r, cap, 8, S) - S["target_profit"]) < 0.01


def test_low_value_items_cannot_qualify():
    # a $25 item can't make $20 even if free
    assert h.max_price(25, 8, S) < 0


def test_bin_deal_and_reject():
    it = item("Patagonia Retro-X jacket", {"used": 110})
    cap = h.max_price(110, 8, S)
    d = h.evaluate(listing("Patagonia Retro X Fleece Jacket Mens L", cap - 1), it, "bin", CFG, NOW)
    assert d and d["profit"] >= 20, d
    assert h.evaluate(listing("Patagonia Retro X Fleece Jacket Mens L", cap + 1), it, "bin", CFG, NOW) is None


def test_title_rules():
    it = item("Patagonia Retro-X jacket", {"used": 110})
    for bad in ("Patagonia Retro X Fleece Vest Mens", "Womens Patagonia Retro X Jacket",
                "Patagonia Retro X Jacket stained", "North Face like Patagonia Retro X jacket"):
        assert h.evaluate(listing(bad, 5), it, "bin", CFG, NOW) is None, bad


def test_condition_split():
    it = item("Patagonia Retro-X jacket", {"used": 110})  # no new price
    assert h.evaluate(listing("Patagonia Retro X Jacket NWT", 5, cond="New with tags"), it, "bin", CFG, NOW) is None
    it = item("Patagonia Retro-X jacket", {"used": 110, "new": 180})
    d = h.evaluate(listing("Patagonia Retro X Jacket NWT", 60, cond="New with tags"), it, "bin", CFG, NOW)
    assert d and d["per_piece"] == 180


def test_typo():
    it = item("Patagonia Retro-X jacket", {"used": 110})
    t = "Patagoina Retro X Fleece Jacket"
    assert h.evaluate(listing(t, 20), it, "bin", CFG, NOW) is None
    d = h.evaluate(listing(t, 20), it, "typo", CFG, NOW)
    assert d and "typo" in d["kind"]


def test_old_listing_and_bad_seller():
    it = item("Patagonia Retro-X jacket", {"used": 110})
    assert h.evaluate(listing("Patagonia Retro X Jacket", 20, age_h=100), it, "bin", CFG, NOW) is None
    assert h.evaluate(listing("Patagonia Retro X Jacket", 20, fb=1), it, "bin", CFG, NOW) is None


def test_auction_bid_up_to():
    it = item("Patagonia Retro-X jacket", {"used": 110})
    d = h.evaluate(listing("Patagonia Retro X Jacket", 15, opts=("AUCTION",), bid=15, ends_h=1), it, "auction", CFG, NOW)
    assert d and d["auction"] and abs(d["profit"] - 20) < 0.01 and d["profit_now"] > 20
    # ends too far out
    assert h.evaluate(listing("Patagonia Retro X Jacket", 15, opts=("AUCTION",), bid=15, ends_h=10), it, "auction", CFG, NOW) is None


def test_lot():
    it = item("Patagonia Better Sweater", {"used": 70})
    d = h.evaluate(listing("Lot of 4 Patagonia Better Sweater 1/4 Zip Mens", 60, ship=15), it, "lot", CFG, NOW)
    assert d and d["count"] == 4 and d["profit"] >= 20, d
    assert h.evaluate(listing("Lot Patagonia Better Sweater mixed", 60), it, "lot", CFG, NOW) is None


def test_feedback():
    rows = [{"listing_id": "v1|1|0"}, {"listing_id": "v1|2|0"}]
    n = h.apply_feedback([{"event": "message", "message": "bought v1|2|0", "time": 1795000000},
                          {"event": "open"}, {"event": "message", "message": "hello"}], rows)
    assert n == 1 and rows[1]["outcome"] == "bought" and "outcome" not in rows[0]


def test_searches_and_pacing():
    cfg = copy.deepcopy(CFG)
    assert h.build_searches(cfg) == []  # nothing on until sold prices are filled in
    for it in cfg["items"]:
        it["sold"] = {"used": 100}
    sr = h.build_searches(cfg)
    gap = h.round_seconds(sr, cfg)
    per_day = sum(1.0 / e for *_, e in sr) * 86400 / gap
    assert per_day <= S["daily_call_budget"] + 1, per_day
    f, sort = h.filters_for(cfg["items"][0], "bin", cfg, NOW)
    assert any(x.startswith("price:[..") for x in f) and "conditions:{USED}" in f


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
