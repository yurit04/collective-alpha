# 0004: Joining Massive and EDGAR (vinculum)

Date: 2026-11-01. Status: accepted.

## Problem
Market cap, size universes and valuation ratios need Massive's prices and the SEC's share counts
and fundamentals, joined per security and day. The join is where silent errors live: a wrong CIK
attaches another company's numbers; splits, ADR ratios, share classes and unit errors in filings
make share counts disagree with the traded security by factors of 2 to 1,000,000.

## Decision
A core package, `vinculum`, depending on `massive-subspace-stream` and `edgar-cortical-node`. It
rebuilds derived tables nightly (about 30 s) under `~/market_data/vinculum` and serves market cap,
ranks, universes and fundamentals per security.

- **CIK per security, checked.** Massive's point-in-time CIK runs are compared with the SEC's
  name history (new: `EdgarStore.profiles()/names()` from the bulk submissions file). Names are
  matched as token sets with abbreviations expanded. Unknown CIKs held at the same time by
  unrelated stocks are rejected (fund trusts legitimately share one CIK across ETFs, so only
  equity is checked); missing, wrong or rejected CIKs are repaired by a unique name match whose
  company filed periodic reports in that period. 1,415 repaired, 960 rejected.
- **Share counts in the security's units.** All counts are put in today's units
  (`shares_now`) so that market cap = split-adjusted close x shares_now x basis ratio; Massive's
  split factor converts per day. Splits apply to the SEC count unless the count shows otherwise
  (only checked for ADRs and multi-class securities). Unit errors are found by 1000^n steps and
  by implied turnover (dollar volume / market cap): a level moves back to the usual level when
  that is plausible, stays when it is plausible itself, else takes the power of 1000 nearest
  typical turnover (0.3% a day). Turnover is measured against the listed class whose price fits
  the count's units (BRK.A for Class A equivalents).
- **Basis ratio** from Massive's current count (new dataset `ticker_details`): snapped to round
  ratios for ADRs and classes; ordinary single-class stock keeps 1 (flagged when they differ),
  so a disagreement with Massive never rescales a company's whole history.
- **Multi-class companies**: market cap per class is the company's value seen through that
  class's price; `is_primary` (most traded class) carries the company in ranks.
- **Safety net**: while a count is in use, median turnover outside 10^-5.5..3 a day withholds
  the market cap.
- **Universes**: ranked by issuer market cap at the previous month end, like an index; Russell-
  style buckets (1-200, 201-1000, 1001-3000, 3001+).

## Data fixes found on the way (in the upstream packages)
- EDGAR share histories were cut at big splits by the "20x from the latest" rule and at concept
  switches (44% of current stocks lost part of their history; 66 now). Shares now combine
  agreeing concepts and drop only isolated outliers; splits are judged here, where they are known.
- Massive records some scrip dividends as 1 -> N splits (TotalEnergies 1 -> 91.6); read as
  (N-1) -> N. 185 mapped splits (3%) were contradicted by the security's own price (Health Care
  REIT never had a 1-for-165 reverse split) and are dropped; both fixes also correct `adjust=`.

## Checks (`vinculum check`, all passing on the first build)
Known market caps within 10% across splits (AAPL, NVDA, AMZN, CMG), renames (FB), classes
(GOOG/GOOGL once, BRK.A/B), ADRs (TSM), delistings (TWTR); top 10 in 2012 and 2020 match
history; 98.4% of calibrated securities within 10% of Massive's current market cap; CIK on 99%
of common-stock dollar volume since 2010; market cap on 92%+ since 2012; nothing above $8T.

## Why not
- **Massive's market cap / share counts**: today's counts only; history would look ahead.
- **Joining by ticker**: renames and reuse mix companies; the CIK must come per security and day.
- **Fixing share counts inside edgar-cortical-node**: it has no prices or splits (and must not
  depend on Massive); the split- and turnover-aware cleaning needs both.
- **Financial Statement Data Sets** (dimensional, per-class counts): would close the multi-class
  gaps (Berkshire after 2016) but are 100+ files to stitch; left for later.

## Known limits
No market cap before XBRL (2009-2011; foreign filers about 2017-18). ADR and class ratios use
today's counts, so delisted ADRs have none. Ratios only for USD reporters. Size by market cap,
not free float.

## Addendum (2026-11-01): the equity filter was too eager
A code review found three filters silently dropping companies. The when-issued regex matched
every ticker ending in `WI` (597 securities: BBWI, AWI, POWI, SWI). The name screen rejected
MLP "Common Units" (ET, PAA, EPD), ADRs on preferred shares (BBD, ABV), "Senior" and "Unit"
in company names, and BDCs called "Fund": 409 CS/ADRC securities, 84 of them liquid. And
`ranks`/`universe` filtered on `type in (CS, ADRC)`, which excluded every company delisted
before Massive's snapshots carried a type (607 of 3,930 companies on 2012-06-29, among them
Dell, Broadcom, Altera, SanDisk): exactly the acquired names a survivorship-free universe must
keep. Now: a bare `WI` is not when-issued; typed CS/ADRC names are screened only for warrants,
units-with-warrants, notes and baby bonds (the wider screen stays for untyped securities); a
null type passes the `types` filter. The panel grew by 465k rows; all 22 checks still pass.

## Addendum (2026-11-02): public float
Float-adjusted size comes from the 10-K cover's public float (`dei:EntityPublicFloat`, now the
EDGAR concept `public_float`), not from Massive's `float` dataset. Massive's dataset has no
history (it accumulates only from the first download) and may not be in smaller plans.

- **Fraction:** public float divided by the issuer market cap on the float date. A count of
  float shares would need the share count and price on that date and the class mix; the ratio
  needs neither, and covers unlisted classes (Meta's Class B).
- **Use:** from the day after the 10-K, for 400 days, on every class.
- **Sanity limits:**
  - A fraction off by a power of 1000 is fixed, as with share counts.
  - A fraction outside 0.01–1.1 is withheld. Most of these (2,425 of 70k filings) are small
    companies whose share count was out of date on the float date.
- **Results:** the median gap to Massive's free float is 4 points.
- **Rejected alternative:** Massive's per-class definition (Meta 99.9%) is the wrong one for
  sizing the company.
- **Build cost:** the computation needs the built panels, so the build writes them, computes
  the float table, then rewrites the rebuilt years with float columns. That adds about 5 s.
