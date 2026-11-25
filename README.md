# deal-scanner

Finds eBay listings priced low enough to resell for your **target profit** (default $20), and sends them to your phone. Goal: 50 a week.

## How it works

1. **`buylist.yaml`** lists items that reliably sell for $50 or more. For each one you enter what it actually *sells* for (from Terapeak).
2. From that, the scanner works out the **most you can pay** and still make the target after eBay fees, promoted fees, shipping both ways, and sales tax. Each item can have its own costs (see below).
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

Prices written on 11/24/2026 used the lower of "average less 10%" and the median of the last 90 days, matching men's items only. Terapeak's New filter means new, not strictly new with tags. Where a used price is too low to ever clear the profit target (Better Sweater, Denali, Peter Millar quarter zip and Lululemon ABC, all used), that side simply never alerts; the new side still can. Everything runs all year; there is no season switch.

Damage words: listings with flaw, flaws or flawed in the title are skipped (along with stain, holes, torn and the others in `damage_words:` in `buylist.yaml`). Whole words only, so "flawless" still alerts. A title saying "no flaws" is skipped too.

RRL: the old combined RRL entry was replaced on 11/24/2026 by RRL jeans, RRL jackets and RRL shirts, each with its own Terapeak price. Jackets use the 25th percentile because prices run from about $200 to over $1,200. Titles with bear or polo are skipped, so Polo Ralph Lauren bear items never alert, and "Double RL" listings are found as the same item.

Pants: James Perse, Taylor Stitch, Faherty and Outerknown pants were added on 11/24/2026 with your standard costs ($20 profit target, $10 label), not the shorts costs, using Terapeak averages less 10% (or the median if lower). The used sides are so low that the most you could pay is near zero, so mostly the new side will alert.

Outerwear and year round prices: on 11/24/2026 every draft price (from eBay asking prices) was replaced with Terapeak sold numbers from the last 90 days, using the lower of the average less 10% and the median. Arc'teryx Beta used (21 sold) is right at the 20 sold cutoff. Most prices came down, so expect fewer but safer alerts.

Filson: both Filson items (Mackinaw, Tin Cloth) were removed on 11/24/2026. To bring one back, add it again under `items:` (the git history has the old entries).

Carhartt: all four Carhartt items (Detroit, Active, chore coat, double knee) were removed on 11/24/2026 because it is not a favorite brand. To bring one back, add it again under `items:` in `buylist.yaml` (the git history has the old entries).

Shorts brands: on 11/24/2026 all shorts brands were switched on at their Terapeak average from DRAFT_PRICES.md less 10%, leaving out any side under 20 sold. Several used prices are so low that the most you could pay is near zero or below (James Perse, Taylor Stitch, Outerknown, Quiksilver, Faherty used), so those mostly will not alert until a listing is nearly free. Check `python buylist.py --group shorts` for the exact max buys.

Misspellings: an item's `typos:` list is searched separately, about every 6th round, and a hit is treated as the correct name. On 11/24/2026 I added 12 more (for example nanopuff, nupste, tincloth, eric emmanuel) after checking that real listings use them. Each typo costs one extra search per 6 rounds, and the tool slows its rounds a little to stay under eBay's daily limit. Wrong category listings are already found, because searches never filter by category.

Items without a sold price are switched off. With the default costs, anything selling under about **$38** can never make $20. Shorts use cheaper shipping and a **$10 profit target** (set in the `shorts` group), so check their numbers with `python buylist.py --group shorts`.

**Speed vs. coverage:** eBay allows about 5,000 searches a day. With 10 items on, each item is checked about every 5 minutes. With all 25, it's about every 12 minutes. Fewer, better items find deals faster.

## Quiet hours

No alerts reach your phone between 11 PM and 6 AM Pacific. The scanner keeps searching overnight, but each deal it finds is sent to your phone with a delay, so it shows up at 6 AM instead of waking you. Auctions that end before 6 AM are skipped, since they will be over. To change the hours or time zone, edit the `quiet_hours:` line in the settings of `buylist.yaml`. Delete that line to turn quiet hours off. Overnight deals may already be sold by the time you see them.

## Costs: different shipping and profit targets for different items

The `settings:` block at the top of `buylist.yaml` holds the default costs. A kind of item can have its own by joining a **group**:

```yaml
groups:
  shorts:
    costs:
      typical_ship_in: 7     # what you usually pay to have a pair shipped to you
      unknown_ship_in: 7     # guess used when a listing shows calculated shipping
      buyer_shipping: 6.50   # what you charge the buyer
      postage_out: 7.00      # your label cost
      promo_rate: 0.05       # promoted listing rate (0 if you don't promote)
      target_profit: 10      # minimum profit for this group
      lot_postage_out: 25    # one label for a lot
      lot_buyer_shipping: 15 # what buyers pay for a lot

items:
  - name: Birdwell board shorts
    group: shorts            # uses the shorts costs
```

An item can also have its own `costs:` block. The order is: the item's own costs, then its group's, then the defaults in `settings:`. Anything you don't list stays at the default. A misspelled cost name stops the scanner with a clear message rather than being ignored.

**Lots:** by default a lot is charged shipping and the per order fee for every piece, which is cautious. If you ship lots as one parcel, add `lot_buyer_shipping` (what you charge for the whole lot) and `lot_postage_out` (the one label) to the group. Then a lot is charged once, not once per pair. A small lot never costs more than its pairs shipped one by one, so a lot of 2 uses two single labels and only bigger lots use the full lot label. The shorts group uses a $25 label and $15 charged to the buyer for a lot.

**Best Offer:** `best_offer_discount: 0.20` in `settings:` assumes sellers usually accept up to 20% off. A group or item can set its own, or 0 to turn it off. A Best Offer listing priced over your max still alerts when 80% of its price is under your max. The alert says **MAKE AN OFFER** with the amount, and the profit it shows is what you make if they accept. Listings without Best Offer are judged at their listed price as before. It applies to every item.

### Lots

Every shorts brand has `lots: true`, so lots of that brand are searched too (lots of 2 to 20 pairs, each pair valued at 80% of the single pair price). Titles have to say how many ("Lot of 4", "3 pairs"); a lot with no clear count is skipped.

**Mixed brand lots** (like "lot of 8 men's board shorts Patagonia Birdwell") are handled by the `Mixed brand shorts lots` item. Each brand item in the group has a `brand:` word. For a lot, the scanner:

1. finds every brand named in the title,
2. skips the lot if any named brand has no price yet (so a half known lot is never valued from the brands that are known),
3. values each pair at the **lowest** of the named brands' prices, less a 10% haircut for mixed lots (`mixed_lot_factor: 0.9`), then at 80% for lots.

The mixed item switches itself on whenever any shorts brand has a price. Brands that aren't on your list (Volcom, Gotcha) are not seen, so a lot naming only known brands plus an unknown one is valued as if the unknown brand weren't there.

To see the most you can pay for each item: `python buylist.py` (add `--group shorts` for one group, or `--targets 20,15,10` to compare profit targets).

## Adding or changing items

Copy an item block in `buylist.yaml`. You can add:
- `must:` words the title has to include
- `skip:` words that rule a listing out
- `typos:` misspellings, since cheap listings hide behind them
- `lots: true` to also watch lots
- `group:` and `costs:` for different shipping or profit targets (see above)

Turn shorts back on in spring and outerwear back on in fall.

## Recording what you resold

When something you bought sells, open `alerts.csv` on GitHub and enter the sale price in **resale_actual**. The scorecard then shows your real profit next to the estimate. That's how you'll know whether the Terapeak numbers need adjusting.

## Files

| File | What it is |
|---|---|
| `hunter.py` | the scanner (runs from `.github/workflows/hunt.yml`, restarts itself every 5.5 hours) |
| `buylist.yaml` | your items and sold prices |
| `buylist.py` | shows max buy prices per item and rebuilds `BUYLIST.md` (Terapeak links). Options: `--group`, `--targets` |
| `report.py` | the weekly scorecard |
| `alerts.csv` | every alert, with Bought/Wrong taps and resale prices |
| `test_hunter.py` | offline checks for the profit math and matching |
| `scanner.py`, `fast.py`, `config.yaml` | the old version, kept for reference; no longer scheduled |

Secrets (repo Settings → Secrets → Actions): `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `NTFY_TOPIC`.
