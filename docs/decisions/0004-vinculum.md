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

## Addendum (2026-11-02): successors and per-share TTM
- **Successors.** ExxonMobil moved to a holding company on 2026-07-01: a new security (new
  FIGI) and a new CIK. The old CIK's history stopped and the new one had no share count until
  its first 10-Q, so the largest energy company had no market cap for a month and no TTM
  revenue or earnings for up to a year. Since 2015, 70 companies above $2B had a handoff like
  this (BlackRock, Labcorp, ONEOK, Apollo), and 25 more switched CIK on the same security
  (Broadcom, Marvell), losing 154 month-ends of revenue.
  - **Links:** a `successors` table from the security master (last ticker trading as a new
    security within 7 days, with CIK or name evidence and a ~1:1 price) and from the CIK map.
  - **Uses:** the predecessor's counts and floats known before the link carry the successor;
    fundamentals fall back up to two links while the CIK in use has nothing fresh.
  - **What is not linked:** a merger at another exchange ratio. Its predecessor is a different
    share, so linking would misstate both market cap and returns.
  - **Considered:** linking by ticker alone, rejected: tickers are reused by unrelated
    companies.
- **Per-share TTM.** EDGAR can only bracket a split between two share counts, so its per-share
  TTM is left empty across one (often 6-9 months for AAPL in 2020, NVDA in 2024), and the
  previous year's EPS would carry forward. vinculum knows the exact split dates and sums the
  quarters itself: w = EPS x cumulative split product at filing, divided by the product on the
  day of use. NVDA's P/E now tracks market cap / net income through the split.
- **Build:** about 65 s instead of 30 s (successor detection and the larger EDGAR read).

## Addendum (2026-11-03): penny ADRs with a wrong ratio
- **Problem.** A few penny ADRs reached the top 1,000 by issuer market cap because their ADS
  ratio (basis) was wrong: NCNA at $0.81 and about $37k a day shown as a $6.2B company (May
  2023), SVRE at about $8.7B, BDRX at $0.22 shown as about $50B. Trading them in the
  unimatrix-zero backtester cost hundreds of basis points of impact. Their turnover (about
  6e-6 a day) sits just above the safety net's floor of 10^-5.5, so the net missed them.
- **Decision.** Penny ADRs (raw close under $2) are judged against Massive's current market cap
  (the anchor), which is right for these securities: a wrong ratio inflates their history, not
  today. The ratio is one number per security (from today's counts), so a wrong one inflates
  the years above $2 as well.
  - **Thin day:** under $2, shown above $1B, 60-day median dollar volume under $1M, and above
    10x the anchor. Withheld, however few such days a security has.
  - **Implausible basis:** an ADR with 20+ thin days, or with 20+ days under $2 whose pooled
    median turnover is under 10^-4.5 a day and median market cap above 10x the anchor, gets
    basis `implausible` and no market cap on any day.
- **How it got there (four builds; the data store was not reachable from where the rule was
  written, so each version was checked by a build on the data machine).**
  1. *Per share-count period:* median price under $2 and median turnover under 10^-4.5 over
     each period one SEC count was in use. 2,491 security-days withheld; SVRE out of the top
     1,000; MFG rank 260 and JHX rank 636 (2023-05-31) unchanged. But NCNA stayed in the top
     1,000 on 909 days and BDRX on 175: in their periods above $2 (NCNA traded at $3-10 for
     years) the wrong ratio still showed them at $15-60B. Ten ADRs had 420 days under $2 above
     $5B: five traded under $1M a day (BDRX, RDHL, NCNA, XHG, YMT), five heavily, confirming
     that a price rule alone would remove real companies.
  2. *Pooled penny turnover per security:* 4 bases implausible, 3,995 days withheld; NCNA and
     BDRX out. But SVRE and XHG pooled at 4e-5, just above the floor: SVRE back in the top
     1,000 on 231 days (up to $29.5B), XHG at $116B; RDHL (inflated in one stretch only) and
     YMT not caught.
  3. *Plus thin days (absolute dollar volume):* 14 bases implausible, 14,104 days withheld,
     every check passing. Reviewing the 14 by name: 12 were small companies worth $1M-1.1B
     today (NCNA, BDRX, SVRE, RDHL, JZ, YMT, TC, ARBK, XHG, UXIN, LOT, AHG) shown at billions, but
     two were real companies with a correct ratio: Enel Chile (ENIC, $5.9B, 50 shares per ADS)
     and Tuya (TUYA, $1B). Both dipped under $2 while trading under $1M a day in the US
     because they trade mostly at home, so "no real $1B+ company has a penny ADR that thin"
     was false.
  4. *Plus the 10x anchor (this version):* ENIC and TUYA stay within about 1-2x of today's
     value on their penny days, while NCNA ($6.2B vs $4M today) and BDRX ($50B vs $0.8M) sat
     thousands of times above it. The ones worth more today (XHG $161M, LOT $259M, UXIN $199M,
     AHG $1.1B) may keep some days; the build shows which.
- **Rejected alternatives.**
  - A stricter turnover floor for all ADRs: real ADRs that trade mostly abroad sit at
    1e-5..1e-4 of their company-wide value in the US (Chinese SOEs whose count includes A and H
    shares, Japanese banks, James Hardie); a floor high enough to catch the junk with margin
    would remove them.
  - An implied ordinary-share price: it needs the ordinary shares per ADS, which is exactly the
    quantity that is wrong.
  - Keying on basis status (`calibrated_raw`/`unknown`): ADRs with `unknown` basis have no
    market cap already, and all 14 flagged ratios were `calibrated`.
  - A price rule alone ("no ADR under $2 above $5B"): it would remove Lloyds and Ambev.
  - Dollar volume without the anchor: removes Enel Chile and Tuya (version 3).
- **Checks.** No ranked ADR under $2 above $5B on a median dollar volume under $1M a day (the
  detail shows each offender's basis, penny days, turnover and thin days); NCNA, SVRE and BDRX
  never in the top 1,000; MFG and JHX in the top 1,000 on 2023-05-31; ENIC and TUYA keep a
  market cap on at least 90% of their penny days.
- **Effect.** `vinculum build` prints the security-days only these rules withheld
  (`penny_adr_days` in `vinculum status`); `basis()` shows `penny_days`, `penny_turnover`,
  `penny_market_cap` and `thin_days` per ADR.
- **Results (version 4, build 33.5 s).** 10 ADR bases `implausible` (14 in version 3: ENIC,
  TUYA and two others released); 9,611 security-days withheld by the ADR rules alone. Every
  `vinculum check` passes: NCNA, SVRE and BDRX never in the top 1,000; MFG rank 260, JHX rank
  635; ENIC keeps 173 of 173 penny days, TUYA 540 of 540; 3 ADRs under $2 above $5B (130 days),
  none trading under $1M a day; calibrated securities within 10% of Massive's current market
  cap: 98.4% of 4,407.

## Addendum (2026-11-03, later): ADSs typed as common stock, carried-over counts
- **Problem.** The unimatrix-zero cost warnings found twelve more companies shown at $2-14B
  in the top 1,000:
  - ten ADSs valued at one ordinary share per ADS: FANH, PLL, MFH, TAOM, SOS, SPI, MOB, QH,
    ISS, TCL;
  - two US share-count errors: Biolase at $6.3B in 2024 and Aradigm at $4.9B in 2014.

  The penny-ADR rules did not see the ADSs: they key on Massive's type ADRC, and these were
  typed CS (Taomee, iSoftStone, Tata Communications) or re-typed CS after converting to
  ordinary shares (Fanhua, Mercurity, SOS, Quhuo, Mobilicom, Piedmont Lithium, SPI). The same
  gap valued Youku, typed CS with 18 shares per ADS, at $43B instead of $2.4B.
- **Decisions.**
  - **ADR by history:** ADRC/GDR/NYRS in any month-end snapshot or an ADR-style name in any
    snapshot; 2,526 security names match. This set gets all the ADR handling.
  - **Ratio from the name for delisted ADRs**, dated to the latest SEC count. Honda's ADS was
    1 share until its 2023 3:1 split; its counts end in 2020, so 1, not 3.
  - **No stated ratio means no market cap** (`unknown`) instead of an assumed 1.
  - **Converted ADSs keep 1** when their latest name says ordinary shares or common stock.
  - **Turnover per own year:** levels are judged only on years the security traded. Biolase's
    post-2022 security was judged on its predecessor's prices, which are in other units, and
    every count was multiplied by 1,000.
  - **Inherited counts:** a count dated before a security's first trade that is more than 3x
    off its first own count is dropped. Aradigm's pre-split count met post-split prices; CAAP's
    pre-IPO 1.5B (160M real) is dropped the same way.
- **Rejected.**
  - *Foreign-filer status (20-F) as the ADR test:* many 20-F filers list ordinary shares
    directly (ICL, CAAP).
  - *A price-to-book or price-to-sales screen:* it would also remove real squeezes (Regencell)
    and asset-light companies.
  - *An assumed ratio of 1 for ADRs with no stated ratio:* most delisted ones were not 1
    (Taomee 20, Focus Media 5, Tata 2), so it would keep most of the errors.
- **Results (build 34.9 s; all 43 `vinculum check` entries pass, 5 of them new).**
  - The twelve are never in the top 1,000; where a value remains it is near reality: ISS $0.27B,
    PLL $0.06B, BIOL $6M, MOB $83M, QH $18M. FANH and SPI are withheld (their bases fail the
    penny-ADR rules), and TAOM, TCL and ARDM have none.
  - Honda (2020) $39.5B, Youku $2.4B. Real CS-typed ADSs keep their caps: Advantest, Novartis,
    HSBC, SAP, Rio Tinto, Nidec.
  - Bases: `name` 120 (Sony, ABB, Canon, HDFC Bank, NTT, BHP, Vale and others now have a market
    cap), `unknown` 395 -> 694, `implausible` 10 -> 17.
  - Security-days with a market cap 17,216,668 -> 17,241,306; 186,018 changed across 646
    securities (64,388 lost, 89,026 gained, 32,604 different).
  - Top-1,000 month-end memberships: 2,014 of 201,398 left, 2,034 entered. Real US companies
    near rank 1,000 (Silicon Labs, Littelfuse, EMCOR) left in some months only because large
    ADRs now ranked above them; their own caps did not change.
  - Calibrated securities within 10% of Massive's current market cap: 98.3% of 4,417.

### Follow-up the same day: counts across a reorganization (LATAM, SK Telecom, Ctrip)
- **Problem.** The first version of the name ratios showed LATAM's old ADS (BBG000JJBNH6) at
  $1.7-2.4T in 2020. LATAM issued 1,000 times its shares in its 2022 reorganization (606M to
  605B). For ADRs, level alignment treats a round step as an ADS ratio change and scales every
  earlier count to the latest level, so the old counts were multiplied by 1,000. A screen first
  added for it (ADR_GIANT, below) withheld 55 days but left one day at $666B, which the new
  check caught. The same alignment had put SK Telecom's old ADS at $45-58B for 2018-21, aligned
  to counts after its 2021 split and after that security stopped trading.
- **Decisions.**
  - **Counts after the last trade:** dropped when more than 3x off the security's last count
    before it, mirroring the rule for counts before the first trade. They never set that
    security's market cap; they only distorted its alignment.
  - **ADR issuance:** across a step of about 1000^n, an ADR level that is plausible as filed is
    not aligned if alignment would put its turnover under 10^-5.5. Only 1000^n steps are judged
    this way. Turnover is measured before the basis, so for an ADR it is off by the ADS ratio.
    A first version that judged every ADR step this way misfired on Can-Fite (62x) and Puhui
    (293x), both ordinary ADS ratio changes.
  - **ADR_GIANT stays as a net:** an ADR day above $50B whose 60-day median dollar volume is
    under 1e-5 of the market cap is withheld. It withholds nothing now.
  - **Ctrip override (basis 8):** Massive's name "1 ADR TO 4 ORD SHS" is reversed (4 ADSs per
    share, 8 after the 2015 2-for-1 ADS split). The old alignment to Trip.com's post-2021 counts
    had hidden this by luck; without it, Ctrip read $0.2-0.7B. With the override it is $5.8B
    (2011) and $22B (2017).
- **Rejected.**
  - *Turnover judging every ADR step:* see Can-Fite and Puhui above.
  - *Relying on ADR_GIANT alone:* it hid the symptom on 55 days and missed a $666B day.
- **Results (all 47 `vinculum check` entries pass).** The new entries are: LTM, CTRP and SKM
  in plausible ranges, and LTM never above $50B.
  - LTM is $5.5B in June 2019 and at most $7.3B.
  - SK Telecom is $15-19B for 2018-21.
  - pSivida/EyePoint gained a market cap ($0.03-0.17B, where it had read $5-35M), and so did
    Genetic Technologies ($20M).
  - One SPAC is worse: DNAA, whose counts are in thousands. The old alignment to a later
    company's counts had fixed it by chance; it now reads $0.4M instead of $0.38B (2021-22).
    That is too small to reach any size universe.
  - Against the build before this addendum: 17,216,668 -> 17,241,509 security-days with a
    market cap; 184,243 changed across 648 securities. Top-1,000 month-end memberships: 2,060
    of 201,398 left and 2,080 entered (Ctrip is among those entering).

### Rejected the same day: ADS ratios from Massive's past share counts
- **Problem.** 266 ADRs with SEC share counts have no basis (`unknown`), 46 of them trading
  over $5M a day, including ones still listed (Baidu, Vodafone, Itau, Lloyds, Bilibili). Their
  SEC counts stop years ago (Baidu's in 2010), so they cannot be calibrated against Massive's
  current count, and their names state no ratio. This predates the addendum above; it only
  moved delisted ADRs with no stated ratio from an assumed 1 to `unknown`.
- **Tried.** Massive's ticker details as of a past date (`/v3/reference/tickers/{t}?date=`)
  return share counts. The idea was to calibrate each ADR at the month end of its latest SEC
  count, putting the dated count into today's units with the security's later splits.
- **Validation (go if >= 90% agree).** The test re-calibrated ADRs already calibrated on today's
  counts, at one of their past SEC counts:
  - 87 of 100 agreed for counts from 2020-22;
  - 19 of 38 agreed for counts from 2009-16.

  The older counts are often in ordinary shares rather than ADSs: NetEase 2015 gave 1 instead of
  0.2, and JD and Huazhu were off too. The unknown ADRs' counts are mostly from 2008-16, so it was
  a no-go. No code was kept.
- **Instead.** The gap is documented under Limits. Overrides in `basis_overrides.csv` (one
  documented row each) fix single names when a study needs them.

### Follow-up: overrides for the large unknown ADRs, N-for-M names
- **Overrides.** 39 new rows in `basis_overrides.csv` for unknown ADRs trading over ~$3M a day.
  Each ratio is the documented ADS ratio, checked against a known company value (a buyout price,
  a year-end value). Unknown ADRs with SEC counts trading over $5M a day: 46 -> 16.
- **Left out.** A ratio does not help where the counts are wrong for the security: counts
  missing share classes (Itau, Bancolombia, CEMIG, Shell A/B), pre-IPO counts (iQIYI, Qunar),
  counts only after the last US trade (America Movil, VimpelCom, Telefonica Brasil, Luckin),
  and GRUB, one security for Grubhub's stock and then Just Eat Takeaway's ADS (ratio 5 would
  have shown Grubhub at $66B).
- **Names giving N ADSs for M shares.** "each four representing one Class A" (DiDi) was read as
  1 share per ADS. `adr_ratio` now reads M / N for "each N [ADSs] representing M" and "N ADS
  REPTG M". Five securities changed: DiDi 1 -> 1/4 per ADS (basis 4; $5B in May 2022, not
  $1.3B), Jianpu, Dingdong, Onion Global, Sunlands. Qihoo's "ADS RPSTG 3 CL A SHS" (really 2 for
  3) is an override: the name alone cannot say it.
- **Reversed names.** Scanned all 58,749 month-end names. The only other "1 ADR TO N" name is
  Daqo's "1 ADR TO 25 ORD SHS": right for its time, and Daqo is calibrated anyway.
- **Checks.** Known values for BIDU 2010, QIHU 2015, VOD 2020, SHPG 2018 and DIDI 2022.
