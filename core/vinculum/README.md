# vinculum

Core module of collective-alpha: the link between Massive securities and SEC EDGAR companies.
It depends on `massive-subspace-stream` (prices, the security master) and `edgar-cortical-node`
(share counts, fundamentals) and builds what neither can alone:

- a **checked CIK for every security and day** (Massive's own CIKs are sometimes wrong);
- **share counts in the security's own units** (splits, ADR ratios, share classes, unit errors);
- a **daily market cap** for every stock and ADR since XBRL began (2009-2011), point in time;
- **size universes** (top N, large/mid/small/micro), survivorship free;
- **point-in-time fundamentals per security**, with valuation ratios;
- **public float** per company from its 10-K, and float-adjusted market cap and size ranks.

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
v.ranks("2020-12-31")                                     # every company, ranked by market cap
v.fundamentals(["revenue", "net_income", "equity"], start="2015-01-01", freq="1m")
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

1. Splits on the security are applied to the SEC's count, except where the count shows they did
   not change it (ADR ratio changes; a class whose count is in another class's units, like
   BRK.B's 2010 split and Berkshire's Class A equivalents).
2. A count dated before a split but filed after it may already be restated: the basis closer to
   the neighbouring counts wins.
3. Unit errors (a company reporting in thousands for a while) are found by steps of 1000^n and
   by implied turnover (dollar volume / market cap): each level is moved to where turnover is
   plausible. Absurd and isolated values are dropped.
4. Basis ratio from Massive's current count: ~1 for stock (kept at 1 when they differ, flagged
   `loose` within 2x, `disagree` beyond), round ratios for ADRs (TSM 0.2) and classes (BRK.B
   1500). Securities without a current Massive count: stocks assume 1, ADRs get none.
5. Safety net: while a count is in use, an impossible median turnover withholds the market cap
   (`implausible`).

## Results (2026-11-01 build)

- 31M rows (2003-2026), about 4,900 ranked companies on a recent day; built in about 25 seconds.
- CIK on 99% of common-stock dollar volume since 2010; 960 Massive CIKs rejected as wrong, 1,415
  repaired by name (Taser, Quality Systems, Morgan's Foods, ...).
- Market cap on 92-96% of common-stock dollar volume since 2012 (none before 2009: no XBRL).
- Against Massive's current market caps: 98.4% within 10% for calibrated securities, 89% overall.
- Top companies match history in every year (2009 XOM, 2012 AAPL/XOM, 2025-26 NVDA).

## Limits

- No market cap before XBRL (2009 large filers, 2011 small; foreign filers about 2017-18).
- ADR and class ratios come from today's counts: delisted ADRs have no market cap.
- Multi-class companies without a company-wide SEC count after some date (Berkshire after 2016)
  have no market cap from then on; tracking stocks (Liberty) get none.
- Ratios only for USD reporters (no FX conversion).
- Size buckets use market cap by default; `by="float_market_cap"` uses the SEC's public float,
  which counts officers, directors and 10% holders as affiliates (narrower than index free float),
  is measured once a year, and is missing for foreign filers (ADRs, Canadian 40-F filers) and
  before 2012.

## Storage layout

```
~/market_data/vinculum/
  cik_map.parquet  share_counts.parquet  split_attribution.parquet  basis.parquet  public_float.parquet
  market_cap/year=YYYY/part.parquet
  build.json   _logs/
```
