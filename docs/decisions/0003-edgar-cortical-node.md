# 0003: SEC EDGAR fundamentals (edgar-cortical-node)

Date: 2026-11-01. Status: accepted.

## Problem
Massive's financial statements are not point in time: each fiscal period is dated by the
*latest* filing that reported it (often a year after the original) and values may be restated.
Massive also has no history of share counts, so market cap cannot be built from it.

## Decision
A core package, `edgar-cortical-node`, that stores the SEC's XBRL **company facts** (every
numeric fact of every filing, with its own `filed` date) and serves point-in-time queries by
CIK. It depends on nothing in the repo; joining to Massive securities (via the security master's
CIK) is the job of a later package.

- **Source:** the nightly bulk `companyfacts.zip` (1.4 GB) for the first load and a monthly
  safety refresh; nightly, the SEC daily index lists who filed, and only those companies'
  facts are re-fetched (`data.sec.gov/api/xbrl/companyfacts/CIK##########.json`).
- **Storage:** one long table of all numeric facts (not a fixed concept list), partitioned by
  CIK bucket, so adding a concept never means re-reading the bulk file. Exact duplicates only
  are dropped; every filing is kept.
- **Point-in-time rules** (enforced by the query API):
  1. a fact is usable from the day **after** `filed` (filings are often accepted after the
     close, and company facts carry only the date);
  2. per period, the **first-filed** value by default (`policy="latest"` for restated values);
  3. a value counts as missing once its period ended more than 400 days earlier.
- **Series:** a concept map of short names to tags in priority order. Per issuer and period the
  value **known first** wins across tags; priority only breaks ties within a filing (preferring
  the new tag after a switch, as in 2018, dated old periods a year late and cut trailing-twelve-
  month coverage in mid-2018 from 79% to 50%). Year-to-date flows are differenced into quarters
  within one tag (across tags only as a fallback); Q4 = annual minus nine months; semi-annual
  filers get halves; trailing twelve months from four quarters or two halves, available once the
  last was filed. Negative derived periods of non-negative items (revenue, capex, dividends) are
  dropped: they come from restatements between the 10-Q and the 10-K.
- **Shares:** cover page (`dei`) > balance sheet > weighted average basic as tie-break; main
  concept per issuer (most filings), gaps filled from concepts agreeing within 1.5x, isolated
  outliers dropped. (Until 2026-11-01: one concept only and values more than 20x from the latest
  dropped, which cut 44% of current stocks' histories at big splits and concept switches; see
  0004.) Multi-class
  issuers (Meta, Alphabet) have no cover-page count in company facts (per-class values are
  dimensional and excluded), so they fall back to the weighted average of all classes.
- **SEC access:** a User-Agent with a contact address is required. The default is a placeholder;
  the user may set their own contact in their local `collective.toml` (outside the repo).
  At most about 8 requests per second, with backoff on 429 and 5xx.

## First load (2026-11-01)
126M facts from 20,537 companies in 0.52 GB of Parquet (the bulk file is 1.41 GB); download and
conversion take under two minutes. 9% of quarterly revenues were later restated by more than
0.5%. Against Massive for the 300 most traded stocks: revenue agrees in 86% of quarters, and
Massive's filing date is later than the original filing in 91% (median: 365 days).

## Why not
- **Massive financials only:** not point in time (see 0001 notes and notebook 04).
- **Financial Statement Data Sets (quarterly bulk):** include acceptance timestamps and
  dimensional facts, but are 100+ files to stitch; company facts cover what we need first.
  They stay an option for exact acceptance times and per-class share counts.
- **Only security-master CIKs:** the bulk file contains every company anyway; filtering would
  make this package depend on Massive.

## Company profiles (added 2026-11-01)
The bulk submissions file (1.6 GB, monthly; filers re-fetched daily) gives profiles and dated
name histories for 48,000 companies that filed periodic reports or list a ticker. vinculum uses
them to check and repair Massive's CIKs; SIC codes give an industry for delisted companies too.

## Convenience translations (added 2026-11-02)
About 416 foreign filers (most Chinese ADRs, plus TSMC, Toyota and SAP) report some periods in
their own currency and in US dollars in the same filing. Series kept one value per period by
filing date and tag priority, so the two currencies tied and the winner was arbitrary: Alibaba's
revenue could come back in dollars and its net income in RMB, and vinculum then computed a 38%
earnings yield. Now, within one filing, the company's reporting currency (the currency most of
its facts for that item are in) wins. A period filed in one currency only keeps it, so a company
that changed its reporting currency keeps both eras. Decided per item, so a result never depends
on which other items were requested.

## Known limits
- XBRL starts in 2009 (large filers) to 2011 (small): no EDGAR fundamentals before that.
- Foreign filers report in their own currency (and `ifrs-full` tags); no FX conversion here.
- As-reported per-share values and share counts are not split-adjusted.
- No analyst estimates, earnings surprises or transcripts.
