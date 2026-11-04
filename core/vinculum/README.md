# vinculum

Core module of collective-alpha: the link between Massive securities and SEC EDGAR companies.
It depends on `massive-subspace-stream` (prices, the security master), `edgar-cortical-node`
(share counts, fundamentals) and `finra-sensor-array` (short data), and builds what none can
alone:

- a **checked CIK for every security and day** (Massive's own CIKs are sometimes wrong);
- **share counts in the security's own units** (splits, ADR ratios, share classes, unit errors);
- a **daily market cap** for every stock and ADR since XBRL began (2009-2011), point in time;
- **size universes** (top N, large/mid/small/micro), survivorship free;
- **point-in-time fundamentals per security**, with valuation ratios;
- **public float** per company from its 10-K, and float-adjusted market cap and size ranks;
- **FINRA short interest and short sale volume per security**, as a share of market cap and float.

Design: `docs/decisions/0004-vinculum.md`.

## Setup

```bash
uv sync --all-packages
uv run vinculum build              # ~30 s; needs the security master, EDGAR facts and profiles
uv run vinculum check              # known market caps, classes, top companies, Massive agreement
uv run vinculum service install    # nightly build, Tue-Sat 07:45 (after both data syncs)
```

Inputs: `subspace identity build`, `subspace backfill rest ticker_details` (Massive's current
share counts, used to set ADR and class ratios), `cortical backfill` and `cortical profiles`.
Everything is rebuilt from those stores; nothing is downloaded.

## Querying

```python
from vinculum import Vinculum

v = Vinculum()
v.market_cap("AAPL", start="2015-01-01")                 # daily, point in time
v.market_cap("META", start="2012-01-01", by="security")  # follows the company across renames
v.universe("2015-06-15", top=500)                         # the 500 largest companies that day
v.universe("2015-06-15", size=["small", "micro"])         # ranks 1001+
v.universe("2015-06-15", top=500, by="float_market_cap")  # ranked on float-adjusted size
v.public_float("WMT")                                     # every 10-K's public float and fraction
v.short_interest("GME", start="2020-12-01")               # by publication date, % of market cap and float
v.short_volume("AAPL", start="2024-01-01")                # daily off-exchange short sale volume
v.ranks("2020-12-31")                                     # every company, ranked by market cap
v.fundamentals(["revenue", "net_income", "equity"], start="2015-01-01", freq="1m")
v.fundamentals(["eps_diluted"], tickers="NVDA")           # TTM EPS in each day's share units
v.successors()                                            # holding-company reorganizations, CIK changes
v.cik_map(); v.share_counts("BBG000B9XRY4"); v.basis("BBG000B9XRY4")
v.scan()                                                  # lazy polars over the whole table
```

Every method takes `backend="pandas"`. CLI: `vinculum show AAPL`, `vinculum universe 2020-06-30
--top 20`, `vinculum status`.

### Market cap

`market_cap = split-adjusted close x SEC count in today's units x basis ratio`, using the latest
SEC count usable that day (the day after filing; stale after 400 days). Columns include the raw
`close`, `shares` (in that day's units), `shares_end` / `shares_filed` (which count), `basis`,
`issuer_market_cap` (the whole company, valued through its primary class) and `is_primary`
(the company's most traded class; size ranks use only these rows, so GOOG and GOOGL count once).

The panel holds common stock and ADRs: Massive's CS/ADRC/OS types plus old untyped securities
whose name looks like equity. MLP units and preferred-share ADRs (Energy Transfer, Banco
Bradesco) count as equity when Massive types them so; warrants, SPAC units, notes and baby
bonds are left out by name, when-issued lines (HONw, DHR.WD) by ticker. `ranks()` and
`universe()` keep securities without a recorded type (companies delisted before Massive's
snapshots carried one, such as Dell or Broadcom); filter on `types` only to narrow further.

### Fundamentals

`fundamentals(items, ...)` takes any of edgar-cortical-node's 31 items (`CONCEPTS`). Flows are
trailing twelve months, instants the latest balance sheet, both missing once their period ended
more than 400 days earlier. `public_float` ages from its filing instead (it is measured months
before the 10-K). Per-share items (EPS) are trailing twelve months in the security's units on
each day. Each quarter is brought to one share basis with the security's own splits before
summing, so NVDA's EPS carries through its 2024 split, and at fiscal year ends the reported
annual figure is used. For ADRs, EPS is per ordinary share in the reporting currency, not per
ADS. Ratios (`earnings_yield`, `book_to_market`, `sales_to_price`, `cash_flow_yield`) are over
the issuer market cap, for USD reporters.

### Successors

The same company sometimes continues under a new security or CIK. `successors()` lists both:

- **kind `security`**: the ticker moves to a new security within 7 days, the two share a CIK
  or a name, and the first new close is within 0.8-1.25x of the last old one (a 1:1 exchange).
  Examples: ExxonMobil's holding company in July 2026, BlackRock in October 2024, Labcorp and
  ONEOK. One-day placeholder listings on the switch day are skipped.
- **kind `cik`**: the same security moves to a new CIK (Alphabet 2016, Broadcom, Medtronic).

A new CIK has no share count for a few weeks and no TTM flows for up to a year. Until it has
its own, the predecessor's last share counts (`shares_tag` starts with `successor:`), public
float and fundamentals (up to two links back) are used, while still fresh. ExxonMobil keeps its
market cap, rank and P/E through July 2026 instead of dropping out for a month. `price_ratio`
lets a backtest link returns across a `security` handoff.

### Public float

Each 10-K's cover states the market value of the common equity held by non-affiliates
(`dei:EntityPublicFloat`), as of the last business day of the second fiscal quarter. Dividing it
by the company's market cap on that day gives the **float fraction**. That needs no share count
of its own, and it covers every class, listed or not: Meta's unlisted Class B is outside its
float. From the day after the 10-K it applies to every class of the company, for up to 400 days:

- `float_fraction`, `float_date`: the latest usable fraction and the date it was measured;
- `float_market_cap = market_cap x float_fraction`, and `issuer_float_market_cap` for the company;
- `ranks(..., by="float_market_cap")` and `universe(..., by="float_market_cap")` size companies
  by float, as indexes do. Companies without a usable float are left out of those ranks.

A fraction above 1.1 or below 0.01 is withheld (`implausible`). Usually that's a share count
out of date on the float date. A fraction off by a power of 1000 (a filing in thousands) is
moved back (`unit_fixed`). Fractions between 1 and 1.1 count as 1. `public_float()` lists every
filing with its status.

How it compares (2026-11-01 build):
- 54k of 70k filings are usable.
- The fraction is present on 90-95% of common-stock market cap a year, from 2013.
- The median gap to Massive's free-float percentage is 4 points. Massive counts per listed class,
  so it puts Meta at 99.9% where the SEC float says 86%.
- Known controlled companies come out right: Walmart 54%, Dell 40%.

### Short interest and short sale volume

FINRA keys its data by its own symbols: `BRKB` in short interest, `BRK/B` in daily volume.
Each symbol is mapped to the security that held the matching Massive ticker on that date,
through the security master's validity windows. That's how FB's short interest stays with Meta,
and why a reused ticker never inherits another company's numbers.

- On exchange-listed stocks, 99.5% of FINRA's short-interest rows map to exactly one security,
  and none to two.
- What doesn't map is units, warrants and rights, plus a few stocks delisted near the
  settlement date.
- OTC stocks have no securities here.

`short_interest()`:
- **One row per security and settlement date.** `start`/`end` filter on `published_date` by
  default: FINRA releases each report 7 business days after settlement, so that's the date to
  trade on. Use `date_by="settlement"` to filter by measurement date instead.
- **`short_value` = short interest × close** on the settlement date.
- **`si_pct_market_cap` = short_value / issuer market cap.** For single-class companies this is
  shares short / shares outstanding. For multi-class companies it is the class's short position
  against the whole company.
- **`si_pct_float` = short_value / issuer float market cap.** GameStop was at 104% of float on
  2021-01-15.
- `days_to_cover` is FINRA's.

`short_volume()`:
- One row per security and day, usable from the next trading day.
- **`short_ratio` = short volume / off-exchange volume.** It is typically 0.4–0.5 for every
  stock, because market makers sell short to fill buy orders. It is a flow, not a position.
- **`off_exchange_share` = off-exchange volume / all-venue volume** from the bars.

Both return equity securities of the market-cap table only. `finra_sensor_array.FinraStore` has
everything else (ETFs, preferreds, OTC stocks).

### CIK statuses

| status | meaning |
|---|---|
| verified | the SEC knows the CIK under a name matching Massive's (any name in its history) |
| ticker | names differ but the SEC lists the security's ticker for that CIK today |
| unverified | the SEC has no profile for the CIK (mostly funds); kept |
| filled | Massive had no CIK in some snapshots; taken from the same security's next run |
| repaired | Massive's CIK was missing, wrong or shared by unrelated stocks; found by name |
| override | `src/vinculum/cik_overrides.csv` |
| rejected / missing | no usable CIK: no fundamentals, no market cap |

### How share counts are cleaned

0. **Which securities are ADRs.** A security is handled as an ADR if Massive typed it ADRC (or
   GDR/NYRS) in any month-end snapshot, or any of its names reads like a depositary receipt
   ("ADS", "SPONSORED ADR", "American Depositary Shares"). Massive types many ADSs as common
   stock (Novartis, HSBC, Taomee, iSoftStone, Youku), and an ADS that later converted to
   ordinary shares by what it is today (Fanhua was ADRC until 2025). Using the latest type
   alone valued them at one ordinary share per ADS: Youku at $43B instead of $2.4B.
1. Splits on the security are applied to the SEC's count, except where the count shows they did
   not change it (ADR ratio changes; a class whose count is in another class's units, like
   BRK.B's 2010 split and Berkshire's Class A equivalents).
   - A count dated before a security's first trade is dropped when it is more than 3x off the
     security's first count after it. This covers a security that replaced another on the same
     CIK at a reverse split, such as Aradigm's 2014 listing after a 1-for-40: the new security
     has no split of its own, so the old count would meet post-split prices.
   - Likewise, a count dated after a security's last trade is dropped when it is more than 3x
     off its last count before then. These counts belong to whatever came next on the CIK and
     never set that security's market cap, but they could pull its earlier counts to their
     level. Before this rule, SK Telecom's old ADS was aligned to the counts after its 2021 split
     ($50B instead of $16B).
2. A count dated before a split but filed after it may already be restated: the basis closer to
   the neighbouring counts wins.
3. Unit errors (a company reporting in thousands for a while) are found by steps of 1000^n and
   by implied turnover (dollar volume / market cap): each level is moved to where turnover is
   plausible. Absurd and isolated values are dropped.
   - Turnover is measured only in years the security itself traded. Biolase's post-2022
     security was once judged on its predecessor's prices, which are in other units; every
     count was multiplied by 1,000, showing $6M as $6B.
   - ADR levels are aligned to the latest one, which is calibrated against Massive. The exception
     is a step of about 1000^n when the level is plausible as filed and alignment would push its
     turnover under 10^-5.5: that is a real share issuance, not a unit change. LATAM issued
     1,000 times its shares in its 2022 reorganization; aligned, its old ADS read $2.4T in 2020
     (now $5-7B). Smaller ADR steps are always aligned: before the basis ratio, turnover is off
     by the ADS ratio and cannot judge them.
4. Basis ratio from Massive's current count: ~1 for stock (kept at 1 when they differ, flagged
   `loose` within 2x, `disagree` beyond), round ratios for ADRs (TSM 0.2) and classes (BRK.B
   1500). Without a current Massive count (delisted):
   - stocks, and former ADRs that converted to ordinary shares, assume 1;
   - an ADR takes the ratio its name states (`name`), as of its latest SEC count. Honda's ADS
     was 1 share until its 2023 split and 3 after, and its counts stop in 2020, so it gets 1.
     Examples: iSoftStone "ADS EACH REPSTG 10 ORD", Sony, ABB, HDFC Bank;
   - an ADR whose names state no ratio gets none (`unknown`): no market cap rather than a wrong
     one. Taomee, Tata Communications, Focus Media, Mindray, Qihoo and Makita lose theirs;
     `basis_overrides.csv` can restore one with a documented ratio. It also corrects names that
     state a ratio backwards: Ctrip's "1 ADR TO 4 ORD SHS" meant 4 ADSs per share (8 after its
     2015 ADS split), so its override is 8 ($22B in 2017, not $0.7B).
5. Safety net: while a count is in use, an impossible median turnover withholds the market cap
   (`implausible`). Penny ADRs (under $2) have two more rules, because a wrong ADS ratio shows
   a micro-cap as a giant (NCNA at $0.81 and $37k a day was shown as a $6.2B company, and at
   $15-60B in its years above $2), while real large ADRs under $2 (Lloyds, Ambev, Santander)
   trade tens of millions of dollars a day:
   - a day under $2 shown above $1B on under $1M a day (60-day median) and at more than 10x
     Massive's current market cap is withheld;
   - an ADR with 20 or more such days, or whose days under $2 (pooled over its history) trade
     under 10^-4.5 of the market cap a day at more than 10x today's value, gets basis
     `implausible` and is withheld on every day, since the ratio is one number per security.
   The 10x anchor keeps real ADRs that trade mostly at home and dip under $2 on thin US volume
   (Enel Chile, Tuya): they stay near today's value, while a wrong ratio shows a micro-cap
   hundreds to tens of thousands of times too large (BDRX: about $50B, $0.8M today).
   ADRs above $2 that trade mostly abroad (Mizuho, James Hardie, ICL) are not affected.
   Any ADR day (any price) shown above $50B whose 60-day median dollar volume is under 1e-5 of
   it is also withheld: no real $50B+ ADR trades that little in the US. This catches a giant a
   stale ratio would create. Since the issuance rule in step 3 it withholds nothing; it stays
   as a net.
   `basis()` shows `penny_days`, `penny_turnover`, `penny_market_cap` and `thin_days` per ADR.
   `vinculum status` reports the security-days this rule withheld (`penny_adr_days`).

## Results (2026-11-01 build)

- 31M rows (2003-2026), about 4,900 ranked companies on a recent day; built in about 25 seconds.
- CIK on 99% of common-stock dollar volume since 2010; 960 Massive CIKs rejected as wrong, 1,415
  repaired by name (Taser, Quality Systems, Morgan's Foods, ...).
- Market cap on 92-96% of common-stock dollar volume since 2012 (none before 2009: no XBRL).
- Against Massive's current market caps: 98.4% within 10% for calibrated securities, 89% overall.
- Top companies match history in every year (2009 XOM, 2012 AAPL/XOM, 2025-26 NVDA).

## Limits

- No market cap before XBRL (2009 large filers, 2011 small; foreign filers about 2017-18).
- ADR and class ratios come from today's counts or, for delisted ADRs, the ratio in their
  name; a delisted ADR whose name states none has no market cap.
- The same holds for an ADR whose SEC share counts stopped years ago, even if it still trades:
  its counts cannot be calibrated against Massive's current one, so it is `unknown` unless its
  name states a ratio. Examples: Baidu (counts end 2010), Vodafone, Itau, Lloyds, Bilibili. In
  the 2026-11-03 build, 266 ADRs with SEC counts are `unknown`, 46 of them trading over $5M a
  day. A documented row in `basis_overrides.csv` restores one. Massive's past share counts
  (ticker details as of a date) were tested as a fix and rejected (0004).
- Multi-class companies without a company-wide SEC count after some date (Berkshire after 2016)
  have no market cap from then on; tracking stocks (Liberty) get none.
- Ratios only for USD reporters (no FX conversion).
- Successors need a ~1:1 exchange: mergers at another ratio (and spin-offs) are not linked, and
  the predecessor's figures are those of the old company (an acquirer's TTM lags the deal, as
  for any acquisition). `market_cap()` by ticker shows the placeholder security on a switch day.
- Size buckets use market cap by default; `by="float_market_cap"` uses the SEC's public float,
  which counts officers, directors and 10% holders as affiliates (narrower than index free float),
  is measured once a year, and is missing for foreign filers (ADRs, Canadian 40-F filers) and
  before 2012.

## Storage layout

```
~/market_data/vinculum/
  cik_map.parquet  share_counts.parquet  split_attribution.parquet  basis.parquet  public_float.parquet
  successors.parquet
  market_cap/year=YYYY/part.parquet
  build.json   _logs/
```
