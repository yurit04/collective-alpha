# Data dictionary

Everything lives under `<data_root>` (default `~/Documents/market_data/massive`) in two tiers.
**raw/** holds vendor files exactly as received and is never edited. **curated/** holds typed,
partitioned Parquet that research code reads. Anything in curated can be rebuilt from raw.

```
raw/flatfiles/us_stocks_sip/<dataset>/YYYY/MM/YYYY-MM-DD.csv.gz   vendor minute and day bars
raw/rest/<table>/<partition>.json.gz                              vendor REST pages
raw/sec/companyfacts.zip                                          SEC bulk archive
curated/<table>/...                                               typed Parquet, see below
manifest.sqlite                                                   what was fetched and converted
logs/ca-YYYY-MM-DD.log
```

Query any curated table through DuckDB, which registers a view per table:

```python
from collective_alpha.storage.catalog import Catalog
cat = Catalog()
cat.tables()                                   # every view
cat.sql("select * from panel limit 5").pl()
```

## The key that ties everything together

Tickers are reused and renamed, so they are not identifiers. `FB` meant Meta until June 2022 and an
ETF from June 2025. Every curated table is keyed by **`security_id`**, a stable identifier taken from
the composite FIGI where the vendor has one, then share-class FIGI, then `CIK:<cik>:<ticker>`, then
`SYM:<ticker>:<first date>`.

| table | grain | notes |
|---|---|---|
| `security_master` | security, validity window | `ticker`, `valid_from`, `valid_to`, `security_id`, name, type, exchange, CIK. Join on `date between valid_from and valid_to` |
| `securities` | security | one row each: tickers used, `first_trade`, `last_trade`, delisting date |
| `security_attributes` | security, month | point-in-time type, exchange and CIK from monthly snapshots |

`collective_alpha.universe.security_master.map_to_security(df, master)` attaches `security_id` to any
frame with `ticker` and `date`.

## Prices

### `panel` — the daily table research should start from

One row per security and session, 13.9 M rows.

| column | meaning |
|---|---|
| `open` `high` `low` `close` `volume` `transactions` | **unadjusted**, as traded |
| `split_ratio` | new shares per old share effective that session, 1.0 if none |
| `dividend` | cash per share going ex that session, in dollars |
| `prev_close` | previous session's close for this security, may span a gap |
| `gap_sessions` | sessions since the previous bar, 1 when consecutive |
| `ret` | **total return**: `(close * split_ratio + dividend) / prev_close - 1` |
| `ret_px` | price return, same without the dividend |
| `adj_factor` `adj_close` | backward split factor and the split-adjusted close |
| `tr_index` | total-return index per security, 1.0 at its first bar |
| `is_last_trade` | final bar of this security, delisting or end of data |
| `suspect_split` | whole-day price level shift with no split on record |

Never compute returns from `close`: it is unadjusted. Use `ret`, or `adj_close` for a price series.

### `day_aggs` and `minute_aggs` — vendor bars

Keyed by ticker, not security. Use them only for ingestion and rebuilds. Minute bars carry `ts` (UTC)
and `ts_ny` (naive New York) and cover 04:00 to 20:00.

### `intraday` — per-session aggregates from minute bars

One row per security and session, 13.4 M rows. The regular session comes from the exchange calendar,
so early closes are handled.

| group | columns |
|---|---|
| coverage | `n_bars`, `bar_coverage`, `n_zero_vol`, `first_bar_min`, `last_bar_min`, `half_day` |
| prices | `open_reg`, `high_reg`, `low_reg`, `close_reg`, `auction_price`, `vwap`, `close_to_vwap` |
| volume | `volume_reg`, `dollar_vol_reg`, `volume_pre`, `volume_post`, `auction_volume`, `volume_open30`, `volume_close30`, and the `share_*` versions |
| volatility | `rv_1m`, `rv_5m`, `hl_range_mean`, `max_abs_1m_ret` |
| spreads | `spread_ar`, `spread_cs`, `spread_est` |
| shape | `or_high`, `or_low`, `or_range_pct`, `close_vs_or`, `ret_open30`, `ret_mid`, `ret_close30` |

Coverage is uneven: the median name has about 70 of 390 possible bars. Estimators needing a dense
series return null below a threshold, so filter on `bar_coverage` rather than assuming presence.

`spread_est` is the canonical round-trip spread estimate: Abdi-Ranaldo floored at one tick. Charge
half of it per trade. **`spread_cs` is inverted on minute bars and must not be used**; it is kept only
as a reference implementation.

`auction_price` is the first print of the closing-minute bar and matches the official close for 97% of
liquid names. The bar's own close can be an after-hours price, so the panel's `close` stays
authoritative.

## Reference and corporate actions

| table | grain | notes |
|---|---|---|
| `tickers` | ticker, snapshot | active and delisted, 36.6 K rows |
| `tickers_pit` | ticker, month | monthly point-in-time snapshots, what resolves symbol reuse |
| `ticker_details` | ticker | SIC code, shares outstanding, exchange, description |
| `ticker_events` | event | ticker changes with dates |
| `splits` `dividends` `ipos` | event | full vendor history; dividends include announced future ex-dates, so filter by as-of date |
| `exchanges` `conditions` `ticker_types` `market_holidays` | small reference snapshots |

## Fundamentals and positioning

| table | grain | notes |
|---|---|---|
| `sec_facts` | CIK, concept, period, filing | SEC XBRL company facts, 7.3 M rows, every filing kept so values are usable from their `filed` date |
| `short_interest` | ticker, settlement date | twice monthly, published with a lag |
| `short_volume` | ticker, session | daily short-sale volume, vendor history starts 2024-02 |
| `news` | article | 750 K articles with vendor sentiment on about 18% |

## Universes

`universes`, partitioned by name and year: one row per date and member with `rank`, `lag_adv`,
`lag_close`, `cap`, `is_new` and `rebalance_date`. Names are defined in `config/universes.toml`.

| universe | rule | members now |
|---|---|---|
| `all_common` | common stock and ADRs on major exchanges, light liquidity floor | ~4,900 |
| `liquid_3000` | adds a $500 k ADV floor and a rank cap | ~2,700 |
| `liquid_1500` | $2 M ADV, $5 price, one class per issuer | ~1,500 |
| `liquid_500` | $10 M ADV | ~520 |
| `cap_1000` `cap_3000` | ranked by market cap from SEC shares | ~1,100 / ~3,100 |

## Features

Six groups under `curated/features/<group>`, joined on `security_id` and `date`. Every value uses
only information known by that session's close.

| group | count | examples |
|---|---|---|
| `price` | 24 | `ret_*d`, `mom_12_1`, `vol_21d`, `parkinson_21d`, `dist_52w_high`, `adv_21d`, `amihud_21d`, `log_price` |
| `size` | 6 | `shares`, `cap`, `log_cap`, `turnover_21d` |
| `fund` | 19 | `revenue_ttm`, `net_income_ttm`, `cfo_ttm`, `earnings_yield`, `book_to_market`, `roe`, `accruals`, `leverage` |
| `short` | 8 | `si_ratio`, `si_days_to_cover`, `si_change`, `sv_ratio_21d` |
| `news` | 7 | `news_count_5d`, `news_count_21d`, `news_sent_5d`, `news_sent_21d` |
| `intra` | 30 | `rv_21d`, `spread_21d`, `close_to_vwap`, `share_auction_21d`, `overnight_21d`, `intraday_21d`, `close_vs_or` |

`ca features list` prints the current set.

## Forward returns

`forward_returns`: `fwd_ret_{1,2,3,5,10,21,42,63}d` plus `fwd_o2c_1d` and `fwd_c2o_1d`. These are what
a position opened at the signal date's close earns. A security that stops trading inside the horizon
compounds to its last bar and then a configurable delisting return. Rows too close to the end of the
store are null, never truncated.

## Coverage caveats worth remembering

| issue | effect |
|---|---|
| Starter plan | no trades, quotes or vendor financial statements |
| `short_volume` | vendor history begins 2024-02, earlier dates are genuinely absent |
| news volume | drops from ~18 K to ~5 K articles a month after mid-2024, a vendor change |
| revenue tags | `revenue_ttm` resolves for about 78% of names; add tags in `data/sec/facts.py` if a study needs more |
| multi-class issuers | some have no undimensioned share count in SEC facts and are absent from cap universes |
| ADRs | SEC reports ordinary shares, prices are per depositary share; a per-issuer ratio corrects this |
| `dividends` | include announced future ex-dates |
