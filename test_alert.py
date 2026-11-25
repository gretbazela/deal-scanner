"""Send one fake alert to your phone with the real buttons, then wait for your tap.

Run it from GitHub: Actions, Test alert, Run workflow. It does not touch alerts.csv, seen.json or any price.
Tap Bought or Wrong on the TEST alert within 5 minutes. The script reads your tap from the feedback channel,
the same way the Hunt does, and sends a second phone message saying what it received.
"""

import os
import sys
import time
from datetime import datetime, timezone

import requests

import hunter as h

WAIT_SECONDS = 5 * 60


def fake_deal(now):
    return {
        "id": f"test-{int(now.timestamp())}", "url": "https://www.ebay.com/", "title": "TEST ALERT: tap Wrong or Bought",
        "item": "TEST ALERT", "kind": "bin", "condition": "Pre-owned", "price": 10.00, "offer": None, "offer_pct": None,
        "ship_in": 5.00, "ship_guess": False, "landed": 16.20, "count": 1, "resale": 60.0, "per_piece": 60.0,
        "profit": 25.0, "profit_now": 25.0, "max_price": 30.0, "auction": False, "ends": None, "best_offer": False,
        "seller": "test", "was": None,
    }


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC is not set, so there is no phone to send to.")
        sys.exit(1)
    now = datetime.now(timezone.utc)
    d = fake_deal(now)
    h.notify(topic, d)
    print(f"Sent the test alert ({d['id']}). Tap Bought or Wrong on your phone within {WAIT_SECONDS // 60} minutes.", flush=True)
    rows = [{"listing_id": d["id"]}]
    since = int(now.timestamp()) - 5
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        time.sleep(10)
        try:
            r = requests.get(f"{h.NTFY}{topic}-fb/json", params={"poll": "1", "since": str(since)}, timeout=20)
        except requests.RequestException as e:
            print(f"  could not read the feedback channel: {e}", flush=True)
            continue
        msgs = [__import__("json").loads(line) for line in r.text.splitlines() if line.strip()]
        if h.apply_feedback(msgs, rows):
            tap = rows[0]["outcome"]
            print(f"Received your tap: {tap}", flush=True)
            h.send_note(topic, f"Test worked: got your {tap.upper()} tap",
                        f"The scanner received your {tap} tap on the test alert.\n\nWHAT TO DO\nNothing. The buttons reach the scanner.",
                        tags=("white_check_mark",))
            return
    print("No tap received.", flush=True)
    h.send_note(topic, "Test alert: no tap received",
                "I did not get a tap on the test alert within 5 minutes.\n\nWHAT TO DO\nIf you tapped Bought or Wrong and "
                "nothing happened, tell Claude what you saw. If you did not tap, run the test again.",
                tags=("warning",))
    sys.exit(1)


if __name__ == "__main__":
    main()
