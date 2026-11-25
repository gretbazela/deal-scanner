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
    for it in cfg["items"]:
        it["sold"] = {}
    assert h.build_searches(cfg) == []  # nothing on until sold prices are filled in
    for it in cfg["items"]:
        it["sold"] = {"used": 100}
    sr = h.build_searches(cfg)
    gap = h.round_seconds(sr, cfg)
    per_day = sum(1.0 / e for *_, e in sr) * 86400 / gap
    assert per_day <= S["daily_call_budget"] + 1, per_day
    f, sort = h.filters_for(cfg["items"][0], "bin", cfg, NOW)
    assert any(x.startswith("price:[..") for x in f) and "conditions:{USED}" in f


def test_group_costs_and_overrides():
    base = dict(CFG["settings"])
    groups = {"shorts": {"costs": {"buyer_shipping": 6.5, "postage_out": 7, "target_profit": 15}}}
    plain = {"name": "x"}
    assert h.costs_for(plain, groups, base) == base  # no group: unchanged
    it = {"name": "y", "group": "shorts"}
    s = h.costs_for(it, groups, base)
    assert s["buyer_shipping"] == 6.5 and s["target_profit"] == 15 and s["fee_rate"] == base["fee_rate"]
    it = {"name": "z", "group": "shorts", "costs": {"target_profit": 10}}
    assert h.costs_for(it, groups, base)["target_profit"] == 10  # the item beats its group


def test_bad_cost_names_are_caught():
    for it, groups in (({"name": "a", "costs": {"postage": 5}}, {}),
                       ({"name": "b", "group": "nope"}, {})):
        try:
            h.costs_for(it, groups, CFG["settings"])
        except ValueError:
            continue
        raise AssertionError("should have refused " + it["name"])


def test_cheaper_shipping_raises_the_max_price():
    s = dict(S)
    cheap = dict(S, buyer_shipping=6.5, postage_out=7.0, unknown_ship_in=7.0)
    assert h.max_price(60, 7, cheap) > h.max_price(60, 7, s)


def test_lot_as_one_parcel():
    s = dict(S)
    one_parcel = dict(S, lot_buyer_shipping=10.0, lot_postage_out=12.0)
    # without lot shipping, a lot of 6 pays shipping and the order fee six times
    assert h.net_total(40, 6, one_parcel) > h.net_total(40, 6, s)
    # a single pair is unaffected by lot settings
    assert h.net_total(40, 1, one_parcel) == h.net_total(40, 1, s)
    cap = h.max_price(40, 7, one_parcel, 6)
    assert abs(h.profit(40, cap, 7, one_parcel, 6) - one_parcel["target_profit"]) < 0.01


def test_item_costs_reach_the_alert_math():
    it = item("Patagonia Retro-X jacket", {"used": 110})
    it["s"] = dict(it["s"], target_profit=5)
    cap_low = h.max_price(110, 8, it["s"])
    d = h.evaluate(listing("Patagonia Retro X Fleece Jacket Mens L", cap_low - 1), it, "bin", CFG, NOW)
    assert d and abs(d["max_price"] - cap_low) < 0.01  # uses the item's $5 target, not the global $20


def test_real_buylist_costs():
    shorts = [i for i in CFG["items"] if i.get("group") == "shorts"]
    assert shorts and all(i["s"]["target_profit"] == 10 and i["s"]["postage_out"] == 7 for i in shorts)
    others = [i for i in CFG["items"] if i.get("group") != "shorts"]
    assert others and all(i["s"]["target_profit"] == 20 and i["s"]["postage_out"] == 10 for i in others)
    assert not any("stussy" in i["name"].lower() for i in CFG["items"])  # cut: only 9 sold in 90 days


def test_eric_emanuel_new_uses_terapeak_price_and_shorts_costs():
    it = next(i for i in CFG["items"] if i["name"] == "Eric Emanuel shorts")
    assert it["sold"] == {"used": 35.0, "new": 68.0}
    cap = h.max_price(68, 7, it["s"])
    assert 32 < cap < 34, cap  # about $33 before shipping and tax at a $10 target
    d = h.evaluate(listing("Eric Emanuel EE Basic Shorts Mens L NWT", cap - 1, ship=7, cond="New with tags"), it, "bin", CFG, NOW)
    assert d and d["profit"] >= 10
    assert h.evaluate(listing("Eric Emanuel EE Basic Shorts Mens L NWT", cap + 1, ship=7, cond="New with tags"), it, "bin", CFG, NOW) is None


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
