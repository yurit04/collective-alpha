# edgar-cortical-node

Core module of collective-alpha. It stores the SEC's XBRL **company facts** (every numeric fact
of every 10-K, 10-Q, 20-F, ... filing, each with its own filing date) under `~/market_data/edgar`
and serves **point-in-time** fundamentals and share counts by CIK. It depends on no other
package in this repo. Design: `docs/decisions/0003-edgar-cortical-node.md`.

Why: Massive's financial statements are dated by the latest filing that reported a period and
may be restated, so backtests on them look ahead. EDGAR keeps the original filings.

## Setup

`tools/bootstrap.sh` at the repo root runs every package's first-time setup in order, then
schedules the nightly run; see the root README's Quick start. By hand:

```bash
uv sync --all-packages
uv run cortical init         # folders, SEC contact check
uv run cortical backfill     # first load: 1.4 GB bulk file, converted in parallel (~10-20 min)
uv run cortical profiles     # company profiles and name history (1.6 GB submissions file)
uv run cortical sync         # daily: only companies that filed since the last sync
uv run cortical status
```

`sync` copes with the SEC's gaps: a weekday whose daily index is missing (a holiday, an index
not posted yet, a throttled request all look the same) is asked for again for 7 days, and a
company whose document cannot be fetched or read fails alone and is retried on the next sync.
A bulk download cut off on the way starts again (3 attempts).

`backfill` and `sync` end by materializing every item's point-in-time series and the share
counts (`series.parquet`, `shares.parquet`, about two minutes), so `series()` and `shares()`
read instead of rebuilding from 100 million facts on every call. `cortical series` rebuilds
them by hand; `cortical status` shows `series_current`.

The SEC requires a contact e-mail in the User-Agent. The default is a placeholder; to use your
own, put it in your local `collective.toml` (outside the repo):

```toml
[edgar_cortical_node]
user_agent = "your-name your@email.example"
# data_root = "~/market_data/edgar"
```

## Querying

```python
from edgar_cortical_node import EdgarStore, CONCEPTS

store = EdgarStore()
store.series("revenue", ciks=[320193])                 # quarters (+ TTM) and fiscal years
store.series(["net_income", "assets"], ciks=[320193, 789019], policy="latest")
store.shares([320193, 1326801])                         # shares outstanding history
store.as_of("2020-06-30", ciks=[320193])                # everything knowable that day
store.facts(320193, tags="us-gaap:Revenues")            # raw facts, every filing
store.companies()                                       # cik, name, facts, last filing
store.profiles([320193])                                # SIC, fiscal year end, tickers, ... (current)
store.names([1326801])                                  # name history: Facebook Inc -> Meta Platforms
```

CIKs can be ints or zero-padded strings. To start from a ticker, get the CIK from the security
master (`massive_subspace_stream.Store().resolve("AAPL", on=...)`), which knows which company a
ticker meant on each day.

### Point-in-time rules
1. A fact is usable from the **day after** it was filed (`available_from`). Company facts give
   only the filing date, and many filings are accepted after the close.
2. Per period, the **first-filed** value (`policy="first"`, default). `policy="latest"` gives
   restated values (fine for live use, look-ahead in backtests).
3. In `as_of`, values whose period ended more than 400 days earlier are treated as missing.

### Series
- **Flows** (revenue, net income, cash flows): one row per fiscal quarter. Quarters are reported,
  or derived: year-to-date figures are differenced (cash flows are usually reported that way),
  and Q4 = annual minus nine months. `ttm` sums four consecutive quarters and is usable once the
  last of them was filed. Semi-annual reporters get `period="half"` rows (H2 = annual minus H1,
  `ttm` from two halves), and reported fiscal years are included as `period="annual"` rows
  (annual-only filers). For items that cannot be negative (revenue, capex, dividends paid), a
  negative derived period is dropped: it means the company restated between its 10-Q and 10-K
  (e.g. discontinued operations), so the two year-to-date figures are on different bases.
- **Instants** (assets, equity, cash, debt): one row per balance-sheet date. `public_float` (the
  10-K cover's market value of common equity held by non-affiliates, in USD) is an instant dated
  the float date, usually the last business day of the second fiscal quarter, months before the
  10-K is filed. About 6% of 10-Ks tag the fiscal year end instead (Caterpillar since 2024);
  those are moved back six months and marked `source="redated"`. Floats dated after the fiscal
  year end are left alone (they match the market value on the date given). Foreign filers
  (20-F, 40-F) do not report it.
- **Per share** (EPS): quarters as for flows (Q4 = annual minus nine months, an approximation:
  share counts differ between the periods). `ttm` is the reported annual figure at fiscal year
  ends, otherwise the sum of four quarters, left empty when the share count jumped (a split)
  between the filings of the first and last quarter. Values are as filed, not split-adjusted;
  `vinculum` sums them across splits with each security's split history.
- Each item maps to several tags in priority order (`CONCEPTS`). For each company and period the
  value **known first** wins, whatever its tag; priority only breaks ties within one filing. (A
  company that changed tags, as many did with the 2018 revenue standard, shows older periods under
  the new tag only in later filings; preferring the new tag would date them a year late.)
- Revenue falls back to bank, REIT and insurance lines when a company tags no total revenue.
- Capex falls back to `PaymentsToAcquireProductiveAssets` (Amazon, about 11% of large companies),
  oil and gas properties and REIT capital improvements; real-estate acquisitions are not capex.
  Dividends paid fall back to `PaymentsOfOrdinaryDividends` (Pfizer).

### Items (`CONCEPTS`, 31)
| Kind | Items |
|---|---|
| flow | revenue, cost_of_revenue, gross_profit, sga, rnd, depreciation_amortization, operating_income, interest_expense, pretax_income, income_tax, net_income, operating_cash_flow, capex, dividends_paid, buybacks, stock_comp |
| per share | eps_basic, eps_diluted |
| instant | assets, current_assets, cash, receivables, inventory, goodwill, liabilities, current_liabilities, debt_current, short_term_borrowings, long_term_debt, equity, public_float |

Definitions to know: `sga` only where one SG&A line is reported (G&A alone is not used, so
Amazon has none); `depreciation_amortization` excludes depreciation-only tags; banks' interest
expense includes deposits; `debt_current` is current maturities of long-term debt and
`short_term_borrowings` is separate (they add up); `long_term_debt` uses `LongTermDebt` first,
which includes current maturities for many filers. Banks and insurers report no current assets
or liabilities. `EdgarStore.concepts()` lists every item's tags and description.

Coverage of common stocks in the Massive universe (revenue usable that day): about 85% from 2018
on, 70% in 2014 (foreign filers started XBRL in 2014-15; closed-end funds and BDCs rarely file
XBRL statements), 29% in mid-2011 (small companies were just starting).

### Shares outstanding
Main concept per company: the one in most filings, with ties broken by cover page
(`dei:EntityCommonStockSharesOutstanding`) > balance sheet (`us-gaap:CommonStockSharesOutstanding`)
> weighted average basic. Gaps are filled from other concepts that agree with it within 1.5x
(a concept that disagrees, such as depositary vs ordinary shares, is never mixed in). Isolated
values far from both neighbours are dropped (unit slips). Splits stay as lasting jumps: counts
are as reported, not split-adjusted (`vinculum` converts them with each security's splits).
Multi-class companies (Meta, Alphabet) report per-class cover counts that company facts leave
out, so they use the weighted average of all classes.

### Company profiles
`cortical profiles` loads the SEC's bulk submissions file (monthly; the daily sync re-fetches
companies that filed): every company that ever filed a 10-K/10-Q/20-F/40-F or lists a ticker
(48,000), with SIC code, fiscal year end, filer category, state, current tickers and the name
history with dates. Values are current, not point in time (the name history is dated).

### Materialized series
With the default `policy="first"`, `series()` and `shares()` read `series.parquet` and
`shares.parquet` when those were built from the current facts: `state.json` records the facts
version (`facts_version`, a counter that `backfill` and every per-company refresh increment) and
the version the tables were built from; a mismatch means a rebuild is pending and reads fall back to building
from the facts (same result, about 2 s per item for 10,000 companies). `policy="latest"` is
always built from the facts. `EdgarStore.series_status()` tells which path a read takes;
`materialized=True` insists on the tables and raises when they are stale.

## Limits
- XBRL starts 2009 (large filers) to 2011 (small): nothing earlier.
- Foreign filers use their own currency (`unit` column) and often `ifrs-full` tags; no FX here.
  Where a filing adds a US-dollar convenience translation, the reporting currency is kept.
- Several listed share classes share one CIK: mapping issuer-level shares to each traded class
  (and not double counting market cap) is done by `vinculum`.
- No analyst estimates, earnings surprises or transcripts.

## Storage layout

```
~/market_data/edgar/
  facts/bucket=NN/part.parquet   all numeric facts, CIKs split into 64 buckets
  companies.parquet              cik, name, facts, last_filed, fetched_at
  profiles.parquet  names.parquet   company profiles and name history (submissions)
  series.parquet  shares.parquet  materialized point-in-time series (every item) and share counts
  state.json                     bulk file date, sync progress, CIKs to retry, facts/series versions
  raw/companyfacts.zip           last bulk download (kept for re-parsing)
  raw/submissions.zip            last profiles download
  _logs/
```
