"""The daily phone check and the warnings.

Once a day (8 AM in your quiet hours time zone) the Hunt sends one plain phone
message, separate from deal alerts, with:
  1. this week's deals against the goal of 50, and the pace needed
  2. items with no alerts in the last 7 days, why their listings were skipped, and what to do
  3. sold prices that are drafts or more than 90 days old, so you know what to check in Terapeak
It also warns you when eBay keeps refusing searches. hunt.yml warns you if the Hunt crashes.

hunter.py calls count() for every listing it looks at and after_round() after every round.
Counts live in state.json under "stats", kept for 14 days.
"""

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import hunter as h

GOAL = 50
SUMMARY_HOUR = 8         # local time the daily check is sent
DEAD_DAYS = 7            # an item with no alert in this many days is listed
MIN_DATA_DAYS = 3        # wait this long before judging items
STALE_DAYS = 90          # sold prices older than this get flagged
KEEP_DAYS = 14
EBAY_WARN_EVERY = 6 * 3600
PASSED = "passed every check"


def tz_of(cfg):
    q = cfg["settings"].get("quiet_hours") or {}
    return ZoneInfo(q.get("timezone", "America/Los_Angeles"))


def day_of(now, cfg):
    return now.astimezone(tz_of(cfg)).strftime("%Y-%m-%d")


# ---------- counting while the Hunt runs ----------

def count(state, cfg, item, listing_id, reason, now, counted):
    """Count one listing for one item, once per day per run, with why it was skipped."""
    day = day_of(now, cfg)
    key = (day, item, listing_id)
    if key in counted:
        return
    counted.add(key)
    st = state.setdefault("stats", {}).setdefault(day, {}).setdefault(item, {"checked": 0, "why": {}})
    st["checked"] += 1
    r = reason or PASSED
    st["why"][r] = st["why"].get(r, 0) + 1


def prune(state, cfg, now):
    keep = {(now - timedelta(days=i)).astimezone(tz_of(cfg)).strftime("%Y-%m-%d") for i in range(KEEP_DAYS)}
    state["stats"] = {d: v for d, v in (state.get("stats") or {}).items() if d in keep}


def after_round(state, cfg, ebay, now, topic, dry):
    prune(state, cfg, now)
    # eBay refused every search this round
    tried = ebay.ok + ebay.failed
    if tried >= 3 and ebay.ok == 0 and now.timestamp() - state.get("ebay_warned", 0) > EBAY_WARN_EVERY:
        title, body = ebay_warning(ebay.last_problem)
        state["ebay_warned"] = now.timestamp()
        if dry or not topic:
            print(f"\n{title}\n{body}", flush=True)
        else:
            h.send_note(topic, title, body, tags=("warning",), priority=4)
    # the daily check, once a day after 8 AM, never during quiet hours
    local = now.astimezone(tz_of(cfg))
    today = local.strftime("%Y-%m-%d")
    if local.hour >= SUMMARY_HOUR and state.get("summary_day") != today and not h.quiet_until(now, cfg):
        title, body = build_summary(h.read_alerts(), state, cfg, now, h.BUYLIST_PATH.read_text())
        state["summary_day"] = today
        if dry or not topic:
            print(f"\n{title}\n{body}", flush=True)
        else:
            h.send_note(topic, title, body)


def ebay_warning(code):
    if code == "429":
        why = "eBay says the daily search limit is used up (code 429)."
        fix = ("Nothing to do now. It usually clears within a day. "
               "If you get this two days in a row, ask Claude to look at the search pace.")
    elif code in ("401", "403"):
        why = f"eBay is turning down the keys (code {code})."
        fix = "Ask Claude to run the Key check. The eBay keys may have expired or been changed."
    else:
        why = f"eBay is not answering searches (code {code or 'none'})."
        fix = "Nothing to do now. eBay usually recovers on its own. If this repeats all day, ask Claude to look."
    return ("Deal Scanner: eBay searches failing",
            f"{why} No deal alerts can come in until it recovers.\n\nWHAT TO DO\n{fix}")


# ---------- the daily check ----------

def week_start(local):
    return (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def sent_local(row, tz):
    return datetime.strptime(row["sent_at"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).astimezone(tz)


def deals_in(row, cfg):
    own = {it["name"]: it["s"] for it in cfg["items"]}
    return h.deal_count(row, own.get(row["item"], cfg["settings"]))


def best_max(it):
    s = it["s"]
    return max(h.max_price(v, s["typical_ship_in"], s) for v in it["sold"].values())


def price_notes(text):
    """The comment written on each item's sold: line in buylist.yaml, by item name."""
    notes, name = {}, None
    for line in text.splitlines():
        m = re.match(r"\s*-\s*name:\s*(.+?)\s*$", line)
        if m:
            name = m.group(1).strip().strip("'\"")
            continue
        if name and re.match(r"\s*sold:", line):
            notes[name] = line.split("#", 1)[1].strip() if "#" in line else ""
    return notes


def note_date(note):
    """The latest date written in a price note (11/24/2026 or 11/2026), or None."""
    found = []
    for m in re.finditer(r"\b(\d{1,2})/(?:(\d{1,2})/)?(\d{4})\b", note):
        mo, d, y = int(m.group(1)), int(m.group(2) or 1), int(m.group(3))
        try:
            found.append(datetime(y, mo, d))
        except ValueError:
            pass
    return max(found) if found else None


def advice(it, st, cfg):
    """One line on why an item had no alerts and what to do about it."""
    t = it["s"]["target_profit"]
    top = best_max(it)
    if top <= 0:
        return (f"can never alert: even free it can't make your ${t:g} target. "
                "Raise its sold price if Terapeak backs it up, or remove it.")
    if not st or not st["checked"]:
        return (f"eBay showed nothing at or under your max buy (about ${top:.0f}). "
                "If Terapeak says it sells higher, raise sold:. If not, remove it so other items get searched more often.")
    why = Counter(st["why"])
    why.pop(PASSED, None)
    if not why:
        return "listings passed every check but none became an alert. Nothing to fix."
    reason, n = why.most_common(1)[0]
    of = f"{n} of {st['checked']}"
    if reason.startswith(("blocked word", "skip word", "title missing", "lookalike")):
        return f"most listings were skipped for {reason} ({of}). If those are real matches, change that item's words."
    if reason.startswith("damage word"):
        return f"most listings mention damage ({of}). Nothing to fix."
    if reason.startswith("over your max"):
        return (f"close calls ({of}): cheap, but shipping and tax tipped them over. "
                "Nothing to fix unless its sold price is too low.")
    if "you only priced" in reason:
        cond = reason.split()[0]
        return f"most were {cond} listings ({of}). Add a {cond} price from Terapeak if it sells 20 or more in 90 days."
    if reason.startswith("seller"):
        return f"most sellers had low feedback ({of}). Nothing to fix."
    return f"most were skipped for: {reason} ({of}). Nothing to fix."


MIN_PATTERN = 8     # alerts in a group before it counts as a pattern
SKIP_SHARE = 0.75   # share of a group's alerts you skipped before it is mentioned


def skipped(r):
    """You skipped an alert if you never tapped it, or tapped Pass. Bought and Wrong are real answers."""
    return r.get("outcome") in (None, "", "pass")


def price_band(r):
    p = float(r.get("landed") or 0)
    return "under $30" if p < 30 else "$30 to $60" if p < 60 else "$60 to $100" if p < 100 else "over $100"


def patterns(rows, days=30, now=None):
    """Groups of alerts you keep skipping: by item, condition, kind and price you pay. Suggestions only."""
    cutoff = None
    if now:
        cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
        rows = [r for r in rows if r["sent_at"] >= cutoff]
    if len(rows) < 20:
        return []
    groups = defaultdict(list)
    for r in rows:
        groups[("item", r["item"])].append(r)
        groups[("condition", (r.get("condition") or "").lower().split()[0] if r.get("condition") else "")].append(r)
        groups[("kind", r["kind"])].append(r)
        groups[("price", price_band(r))].append(r)
    base = sum(skipped(r) for r in rows) / len(rows)
    out = []
    for (dim, val), g in groups.items():
        if len(g) < MIN_PATTERN or not val:
            continue
        share = sum(skipped(r) for r in g) / len(g)
        bought = sum(r.get("outcome") == "bought" for r in g)
        if share >= SKIP_SHARE and bought == 0 and share >= base + 0.1:
            out.append((len(g), dim, val, share))
    out.sort(reverse=True)
    lines = []
    for n, dim, val, share in out[:3]:
        what = {"item": f"{val} alerts", "condition": f"{val} condition alerts",
                "kind": f"{val} alerts", "price": f"alerts where you'd pay {val}"}[dim]
        lines.append(f"• You skipped {share * 100:.0f}% of {n} {what} and bought none.")
    return lines


def names(lst, k=3):
    shown = ", ".join(lst[:k])
    return shown + (f" and {len(lst) - k} more" if len(lst) > k else "")


def build_summary(rows, state, cfg, now, buylist_text):
    tz = tz_of(cfg)
    local = now.astimezone(tz)
    start = week_start(local)
    week = [r for r in rows if sent_local(r, tz) >= start]
    deals = sum(deals_in(r, cfg) for r in week)
    bought = sum(r.get("outcome") == "bought" for r in week)
    wrong = sum(r.get("outcome") == "wrong" for r in week)
    untapped = sum(not r.get("outcome") for r in week)
    days_left = 7 - local.weekday()  # including today
    need = max(0, GOAL - deals)

    out = [f"This week so far: {deals} deals from {len(week)} alerts. Bought {bought}, marked Wrong {wrong}."]
    if need == 0:
        out.append("Goal of 50 reached this week.")
    else:
        out.append(f"Need {need} more by Sunday night, about {need / days_left:.0f} a day.")

    # items with no alerts lately
    active = [it for it in cfg["items"] if it["sold"]]
    cutoff = local - timedelta(days=DEAD_DAYS)
    hits = Counter(r["item"] for r in rows if sent_local(r, tz) >= cutoff)
    stats = state.get("stats") or {}
    recent_days = sorted(d for d in stats if d >= cutoff.strftime("%Y-%m-%d"))
    todo = []
    if len(recent_days) < MIN_DATA_DAYS:
        ready = (datetime.strptime(min(recent_days), "%Y-%m-%d") if recent_days else local.replace(tzinfo=None))
        ready += timedelta(days=MIN_DATA_DAYS)
        item_part = [f"\nITEMS: the check on quiet items starts after {MIN_DATA_DAYS} days of data, "
                     f"around {ready:%a %m/%d}."]
    else:
        merged = {}
        for d in recent_days:
            for name, st in stats[d].items():
                m = merged.setdefault(name, {"checked": 0, "why": defaultdict(int)})
                m["checked"] += st["checked"]
                for k, v in st["why"].items():
                    m["why"][k] += v
        dead = [it for it in active if not hits.get(it["name"])]

        def order(it):
            st = merged.get(it["name"])
            return (best_max(it) > 0, bool(st and st["checked"]), -(st["checked"] if st else 0))
        dead.sort(key=order)
        item_part = [f"\nITEMS: {len(active) - len(dead)} of {len(active)} items alerted in the last {DEAD_DAYS} days."]
        if hits:
            item_part.append("Best: " + ", ".join(f"{n} ({c})" for n, c in hits.most_common(3)) + ".")
        for it in dead[:4]:
            item_part.append(f"• {it['name']}: {advice(it, merged.get(it['name']), cfg)}")
        if len(dead) > 4:
            item_part.append(f"• {len(dead) - 4} more quiet items. Ask Claude for the full list.")
        if dead:
            todo.append("Fix or remove the quiet items above, starting at the top. Ask Claude to make the change.")

    pat = patterns(rows, now=now)
    pattern_part = []
    if pat:
        pattern_part = ["\nWHAT YOU SKIP (suggestions only, nothing changed)"] + pat
        todo.append("Tell Claude whether the skipped groups are a price, wording or taste problem. "
                    "Nothing changes unless you say so.")

    # prices to check in Terapeak
    notes = price_notes(buylist_text)
    draft, old, undated = [], [], []
    for it in active:
        if it.get("mixed"):
            continue
        note = notes.get(it["name"], "")
        dt = note_date(note)
        if "draft" in note.lower():
            draft.append(it["name"])
        elif dt is None:
            undated.append(it["name"])
        elif dt < local.replace(tzinfo=None) - timedelta(days=STALE_DAYS):
            old.append(it["name"])
    price_part = []
    if draft:
        price_part.append(f"• {len(draft)} use draft asking prices, not sold prices: {names(draft)}.")
    if old:
        price_part.append(f"• {len(old)} were checked over {STALE_DAYS} days ago: {names(old)}.")
    if undated:
        price_part.append(f"• {len(undated)} have no date on their price: {names(undated)}.")
    if price_part:
        price_part.insert(0, "\nPRICES TO CHECK IN TERAPEAK")
        todo.append("Check those prices: open BUYLIST.md on GitHub, tap the item's \"sold 90 days\" link, "
                    "and send Claude the average and number sold.")

    if untapped:
        todo.append(f"Tap Bought or Wrong on alerts so the counts stay right ({untapped} this week have no tap; "
                    "leaving ones you passed on untapped is fine).")
    if not todo:
        todo.append("Nothing. Everything looks on track.")

    body = out + item_part + pattern_part + price_part + ["\nWHAT TO DO"] + [f"{i}. {t}" for i, t in enumerate(todo, 1)]
    body.append(f"\nNo daily check by {SUMMARY_HOUR + 1} AM means the scanner is down. Ask Claude to check the Hunt.")
    title = f"Daily check: {deals} of {GOAL} deals this week"
    return title, "\n".join(body)
