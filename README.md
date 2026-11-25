# deal-scanner

Finds eBay listings priced low enough to resell for **$20+ profit**, and sends them to your phone. Goal: 50 a week.

## How it works

1. **`buylist.yaml`** lists items that reliably sell for $50 or more. For each one you enter what it actually *sells* for (from Terapeak).
2. From that, the scanner works out the **most you can pay** and still make $20 after eBay fees, promoted fees, shipping both ways, and sales tax.
3. It asks eBay only for listings at or under that price: new Buy It Now listings, auctions ending in the next 3 hours, misspelled titles, and lots.
4. Each hit goes to ntfy with three buttons:
   - **Bought**: you bought it
   - **Wrong**: the alert was wrong (bad match, fake, damaged)
   - **Sold comps**: opens eBay sold listings for a quick check

   For auctions, the alert tells you the most you can bid.
5. Every alert is saved in **`alerts.csv`**. After each run, the Actions page shows a weekly scorecard: alerts, deals, bought, and marked wrong.

## Adding sold prices from Terapeak

Terapeak is free in eBay Seller Hub (**Seller Hub → Research**). Do this once per item, then about once a month.

1. Open **[BUYLIST.md](BUYLIST.md)**. Each item has a "sold 90 days" link that opens Terapeak's Sold tab for that search. If a link doesn't open, go to Seller Hub → Research and type the item name.
2. Set **Condition** to Pre-owned. Look at three numbers:
   - **Avg sold price**
   - **Total sold**: you want 20 or more in 90 days, so the item sells reliably
   - **Sell-through**: you want 40% or higher
   
   Scroll the sold listings and ignore odd ones, like lots or a different model. If the item fails these checks, leave it off.
3. Write down a slightly cautious number. Rounding the average down about 10% is a good habit.
4. Set Condition to **New with tags** and repeat, if new ones sell often enough.
5. On GitHub, open `buylist.yaml`, click the pencil, and fill in the numbers:
   ```yaml
   sold: {used: 85, new: 140}
   ```
   Leave a side blank to skip it (for example, `sold: {used: 85, new: }`). Then click **Commit changes**.
6. The change takes effect on the next run, within about 5.5 hours. To start right away: **Actions → Hunt →** cancel the running job, then **Run workflow**.

Items without a sold price are switched off. Anything selling under about **$38** can never make $20, so don't bother adding it.

**Speed vs. coverage:** eBay allows about 5,000 searches a day. With 10 items on, each item is checked about every 5 minutes. With all 25, it's about every 12 minutes. Fewer, better items find deals faster.

## Adding or changing items

Copy an item block in `buylist.yaml`. You can add:
- `must:` words the title has to include
- `skip:` words that rule a listing out
- `typos:` misspellings, since cheap listings hide behind them
- `lots: true` to also watch lots

Turn shorts back on in spring and outerwear back on in fall.

## Recording what you resold

When something you bought sells, open `alerts.csv` on GitHub and enter the sale price in **resale_actual**. The scorecard then shows your real profit next to the estimate. That's how you'll know whether the Terapeak numbers need adjusting.

## Files

| File | What it is |
|---|---|
| `hunter.py` | the scanner (runs from `.github/workflows/hunt.yml`, restarts itself every 5.5 hours) |
| `buylist.yaml` | your items and sold prices |
| `buylist.py` | rebuilds `BUYLIST.md` (max buy price per item, Terapeak links) |
| `report.py` | the weekly scorecard |
| `alerts.csv` | every alert, with Bought/Wrong taps and resale prices |
| `test_hunter.py` | offline checks for the profit math and matching |
| `scanner.py`, `fast.py`, `config.yaml` | the old version, kept for reference; no longer scheduled |

Secrets (repo Settings → Secrets → Actions): `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `NTFY_TOPIC`.
