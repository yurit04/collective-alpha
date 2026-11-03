# vinculum (core, depends on massive-subspace-stream, edgar-cortical-node, finra-sensor-array)

Joins Massive securities to SEC EDGAR companies: checked CIKs, share counts in each security's
units, daily market cap, size universes, point-in-time fundamentals per security.
User docs: README.md in this folder. Design: docs/decisions/0004-vinculum.md.

## Public API (`from vinculum import ...`)

| Name | Use |
|---|---|
| `Vinculum(root=None, settings=None, store=None, edgar=None, finra=None)` | all reads and the build; see below |
| `load_settings(config_file=None, **overrides)` / `Settings` | data_root, start_year, build time |
| `SIZE_BUCKETS` | large 1-200, mid 201-1000, small 1001-3000, micro 3001+ (issuer rank) |
| `RATIOS` | ratio name -> EDGAR item (earnings_yield, book_to_market, sales_to_price, cash_flow_yield) |
| `ConfigError` | invalid settings |

`Vinculum` methods (reads take `backend="polars"|"pandas"`):
- `build(years=None)`: rebuild everything (~30 s); `check()`: [(name, passed, detail)]; `status()`
- `market_cap(tickers, start, end, by="ticker"|"security", primary_only=False, lazy=False)`:
  date, security_id, ticker, type, cik, cik_status, close (raw), volume, dollar_volume,
  split_factor, shares (that day's units), shares_end, shares_filed, shares_tag, shares_jump,
  basis_ratio, basis, market_cap, issuer_market_cap, is_primary, implausible,
  float_fraction, float_date, float_market_cap, issuer_float_market_cap (public float from the
  latest 10-K, usable the day after filing, for 400 days)
- `ranks(on, types=("CS","ADRC"), by="market_cap"|"float_market_cap")`: one row per company
  (primary class), rank, size, issuer_market_cap, issuer_float_market_cap; a null `type`
  (delisted before Massive carried types) always passes the `types` filter
- `universe(on, top=None, size=None, types=..., rebalance="monthly"|"daily", by=...)`: ranked on
  the previous month end (monthly) or `on`; rows of `on` for the chosen companies
- `public_float(tickers, start, end)`: per 10-K: cik, float_date, public_float (USD),
  available_from, issuer_market_cap on the float date, ratio, unit_factor, float_fraction, status
- `fundamentals(items, start, end, tickers=None, by=..., freq="1m"|"1d", primary_only=True,
  ratios=True, policy="first")`: any of the 31 EDGAR items; flows as TTM, instants latest, stale
  400 days after the period end (public_float: after its filing); per-share items TTM in the
  security's units that day (quarters put on one basis with the security's splits); where the
  CIK in use has no fresh value, its predecessors' (successors table, up to 2 links); `currency`,
  ratios over issuer_market_cap for USD reporters
- `successors(security_ids)`: security_id, cik, predecessor_id, predecessor_cik, link_date, kind
  ("security": ticker moved to a new security, ~1:1 price; "cik": same security, new CIK),
  ticker, price_ratio, evidence
- `short_interest(tickers, start, end, by=..., date_by="published"|"settlement")`: FINRA short
  interest per equity security: settlement_date, published_date, security_id, ticker, symbol,
  market, type, cik, is_primary, short_interest, change_pct, avg_daily_volume, days_to_cover,
  close, short_value, si_pct_market_cap, si_pct_float
- `short_volume(tickers, start, end, by=...)`: date, security_id, ticker, symbol, type, cik,
  short_volume, short_exempt_volume, off_exchange_volume, volume (all venues), short_ratio,
  off_exchange_share, source
- `cik_map(security_ids)`, `share_counts(security_ids)`, `basis(security_ids)`, `scan()`

CLI: `vinculum build | check | status | show TICKER | universe DATE --top N | service install|remove|status`.

## Internals (private)

```
_config.py    settings ([vinculum] in collective.toml, VINCULUM_DATA_ROOT)
_layout.py    paths, atomic writes, build.json
_names.py     company-name tokens and matching (Massive vs SEC names), looks_equity()
_cikmap.py    CIK runs per security, verification, conflicts, repair by name, overrides, cik_on()
_shares.py    split attribution, restated-basis choice, spikes, unit levels by turnover, basis ratios
_build.py     equity filter, liquidity table, calibration, yearly market-cap panels, public float
              (float_table after the panels, then add_float rewrites them), scan_panels, build()
_store.py     Vinculum (reads, ranks, universes, fundamentals with predecessor fallback, short data)
_pershare.py  per-share TTM per security across splits (w = value x cumulative split product at filing)
_successors.py ticker handoffs and CIK changes, lineage, inherited share counts
_short.py     FINRA symbols -> securities (security master windows), short interest/volume reads
_checks.py    `vinculum check`
_services.py  launchd plist (com.collective-alpha.vinculum.build)
cik_overrides.csv, basis_overrides.csv   manual corrections, one documented row each
```

Invariants:
- Never use a CIK whose status is rejected/missing; never join EDGAR to bars by ticker.
- Market cap uses only SEC counts with available_from <= date and end >= date - 400 days.
- `shares_now` is in today's units of the security: market cap = adjusted close x shares_now x basis.
- Size ranks use `is_primary` rows only (one per company) and `issuer_market_cap`.
- Safety net (`_build.screen`), per SEC count in use: median turnover outside PLAUSIBLE
  (10^-5.5..3 a day) withholds the market cap; so does, for ADRs only, a median raw close under
  $2 with median turnover under 10^-4.5 (ADR_PENNY: a wrong ADS ratio, as NCNA/SVRE/BDRX shown
  at $6-50B). Both set `implausible`. Real penny ADRs above $5B (LYG, ABEV, SAN) trade 1e-4+ a
  day and pass; never tighten the turnover floor for all ADRs (Chinese SOE and Japanese ADRs
  trade 1e-5..1e-4 of their company-wide value in the US).
- The equity filter trusts Massive's type: the name screen for typed CS/ADRC catches only
  warrants, SPAC units, notes and baby bonds (MLP "Common Units" and preferred-share ADRs are
  equity); the wider screen (UNITS, PFD, FUND, SENIOR) applies to untyped securities only.
  When-issued tickers end in a lowercase `w`, `.WD` or `.WI`; never match a bare `WI`.
- Tickers given with `by="security"` that no longer trade resolve to their last holder.
- Float fraction = public float / issuer market cap on the float date (company-wide, every class);
  kept only within FLOAT_RANGE after a 1000^n unit fix; usable from the 10-K's available_from.
  It needs the built panels, so it is computed after them and the rewritten years get the columns.
- Read panels through `scan_panels` (fixed schema): a partial rebuild leaves older files without
  the float columns.
- FINRA rows are mapped to securities by (FINRA symbol of the Massive ticker, date within the
  master's validity window); a symbol matching two securities on a date is dropped. Never join
  FINRA to anything by raw symbol. Short interest is point in time by `published_date`.
- Successor links need CIK or name evidence and a first/last close ratio within 0.8-1.25 (1:1
  exchange); predecessor counts and floats are inherited only if known before the link, and
  fundamentals fall back only while the CIK in use has no fresh value. Never link by ticker alone.
- Per-share values are converted with the security's own splits after their filing; never sum
  per-share quarters filed on different share bases without that conversion.
- Everything under the data root is derived: `vinculum build` may overwrite it all.
