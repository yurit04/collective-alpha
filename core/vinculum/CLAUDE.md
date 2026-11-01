# vinculum (core, depends on massive-subspace-stream and edgar-cortical-node)

Joins Massive securities to SEC EDGAR companies: checked CIKs, share counts in each security's
units, daily market cap, size universes, point-in-time fundamentals per security.
User docs: README.md in this folder. Design: docs/decisions/0004-vinculum.md.

## Public API (`from vinculum import ...`)

| Name | Use |
|---|---|
| `Vinculum(root=None, settings=None, store=None, edgar=None)` | all reads and the build; see below |
| `load_settings(config_file=None, **overrides)` / `Settings` | data_root, start_year, build time |
| `SIZE_BUCKETS` | large 1-200, mid 201-1000, small 1001-3000, micro 3001+ (issuer rank) |
| `RATIOS` | ratio name -> EDGAR item (earnings_yield, book_to_market, sales_to_price, cash_flow_yield) |
| `ConfigError` | invalid settings |

`Vinculum` methods (reads take `backend="polars"|"pandas"`):
- `build(years=None)`: rebuild everything (~30 s); `check()`: [(name, passed, detail)]; `status()`
- `market_cap(tickers, start, end, by="ticker"|"security", primary_only=False, lazy=False)`:
  date, security_id, ticker, type, cik, cik_status, close (raw), volume, dollar_volume,
  split_factor, shares (that day's units), shares_end, shares_filed, shares_tag, shares_jump,
  basis_ratio, basis, market_cap, issuer_market_cap, is_primary, implausible
- `ranks(on, types=("CS","ADRC"))`: one row per company (primary class), rank, size; a null
  `type` (delisted before Massive carried types) always passes the `types` filter
- `universe(on, top=None, size=None, types=..., rebalance="monthly"|"daily")`: ranked on the
  previous month end (monthly) or `on`; rows of `on` for the chosen companies
- `fundamentals(items, start, end, tickers=None, by=..., freq="1m"|"1d", primary_only=True,
  ratios=True, policy="first")`: flows as TTM, instants latest, stale after 400 days, `currency`,
  ratios over issuer_market_cap for USD reporters
- `cik_map(security_ids)`, `share_counts(security_ids)`, `basis(security_ids)`, `scan()`

CLI: `vinculum build | check | status | show TICKER | universe DATE --top N | service install|remove|status`.

## Internals (private)

```
_config.py    settings ([vinculum] in collective.toml, VINCULUM_DATA_ROOT)
_layout.py    paths, atomic writes, build.json
_names.py     company-name tokens and matching (Massive vs SEC names), looks_equity()
_cikmap.py    CIK runs per security, verification, conflicts, repair by name, overrides, cik_on()
_shares.py    split attribution, restated-basis choice, spikes, unit levels by turnover, basis ratios
_build.py     equity filter, liquidity table, calibration, yearly market-cap panels, build()
_store.py     Vinculum (reads, ranks, universes, fundamentals)
_checks.py    `vinculum check`
_services.py  launchd plist (com.collective-alpha.vinculum.build)
cik_overrides.csv, basis_overrides.csv   manual corrections, one documented row each
```

Invariants:
- Never use a CIK whose status is rejected/missing; never join EDGAR to bars by ticker.
- Market cap uses only SEC counts with available_from <= date and end >= date - 400 days.
- `shares_now` is in today's units of the security: market cap = adjusted close x shares_now x basis.
- Size ranks use `is_primary` rows only (one per company) and `issuer_market_cap`.
- The equity filter trusts Massive's type: the name screen for typed CS/ADRC catches only
  warrants, SPAC units, notes and baby bonds (MLP "Common Units" and preferred-share ADRs are
  equity); the wider screen (UNITS, PFD, FUND, SENIOR) applies to untyped securities only.
  When-issued tickers end in a lowercase `w`, `.WD` or `.WI`; never match a bare `WI`.
- Tickers given with `by="security"` that no longer trade resolve to their last holder.
- Everything under the data root is derived: `vinculum build` may overwrite it all.
