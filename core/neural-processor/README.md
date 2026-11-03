# neural-processor

Point-in-time signals per security, built on `vinculum` (fundamentals, market cap, universes,
short interest, successors) and Massive daily bars. About 50 signals in seven groups, each with
its formula, inputs and the direction the literature expects, plus per-date transforms and
composites. No downloads, no stored tables: a panel is computed when asked for.

```python
from neural_processor import Processor, SIGNALS, catalogue, composite, neutralize

p = Processor()                                           # Vinculum(), Store(), EdgarStore()
s = p.panel(["fcf_yield", "roe", "momentum_12_1"], start="2015-01-01", top=1000)
s = p.panel("value", start="2015-01-01", size="mid")      # a whole group, mid caps (ranks 201-1000)
s = p.panel(["book_to_market"], start="2020-01-01", top=500,
            transform="zscore", winsor=0.01, neutralize=["sector", "size"])
composite(s, {"fcf_yield": 1, "roe": 1, "momentum_12_1": 1})   # oriented, z-scored, averaged
catalogue()                                               # name, group, formula, direction, ...
SIGNALS["accruals"].direction                             # -1: lower has done better
```

A panel has one row per security (primary class) and month end (`freq="1m"`) or trading day
(`"1d"`): `date, security_id, ticker, cik, sector, issuer_market_cap`, `rank` and
`size_bucket` when a universe was asked for, then one column per signal.

## Signals

| Group | Signals |
|---|---|
| value | earnings_yield, book_to_market, sales_to_price, cash_flow_yield, fcf_yield, dividend_yield, shareholder_yield, ebit_to_ev, ebitda_to_ev, sales_to_ev |
| profitability | gross_margin, operating_margin, net_margin, roe, roa, gross_profitability, cash_roa |
| quality | accruals, asset_turnover, capex_to_sales, payout_ratio, sbc_to_sales, goodwill_to_assets, rnd_to_sales, sga_to_sales, tax_rate |
| leverage | debt_to_equity, liabilities_to_assets, cash_to_assets, current_ratio, interest_coverage |
| growth | revenue_growth, earnings_growth, eps_growth, asset_growth, share_issuance |
| price | momentum_12_1, reversal_1m, high_52w, volatility_3m, beta_12m, turnover, illiquidity, size |
| short | si_pct_float, si_pct_market_cap, days_to_cover, si_change |

`catalogue()` gives each formula. Rules:

- **Point in time.** Fundamentals are those usable on the row's date (vinculum: first-filed,
  usable the day after filing). Price signals use bars up to that day's close. Short interest
  is the latest FINRA report already published. Growth compares with the values usable a year
  earlier (12 month ends back), not with restated ones.
- **Ratios:** a non-positive denominator gives a missing value, never a sign flip. Anything
  over market cap or EV needs a USD reporter. Missing inputs stay missing; the exception is
  shareholder yield, which needs only one of dividends and buybacks.
- **Enterprise value** = market cap + long-term debt + short-term borrowings − cash.
- **Share units:** EPS and share counts in growth signals are put on one share basis with the
  security's splits.
- **Successors:** a company that moved to a new security (ExxonMobil in July 2026) keeps its
  price and growth history. The predecessor's bars and rows before the link count as the
  successor's, and the return across the link comes from raw closes.
- **Universe:** `top` and `size` use `vinculum.ranks()` on each date (survivorship free);
  `rank_by="float_market_cap"` ranks on float. Or pass `universe` (date, security_id).
- **Price signals:**
  - a daily return above +300% counts as 0 (data breaks such as bankruptcy emergences);
  - momentum: return from 12 months to 1 month ago;
  - reversal: the last month's return;
  - 52-week high: on split-adjusted prices;
  - volatility, turnover and Amihud illiquidity: over 63 trading days;
  - beta: daily, 252 days, against the cap-weighted return of the panel's securities.

## Transforms (per date, never across dates)

- `winsorize(df, cols, q)`: clip at the q and 1-q quantiles.
- `neutralize(df, cols, ["sector", "size"])`: demean within FF12 sector; remove what log
  market cap explains (one regression with the sector dummies).
- `transform(df, cols, method="zscore"|"rank", winsor=, neutralize_on=)`: all three in order.
- `composite(df, weights)`: each signal times its direction, winsorized, z-scored (or ranked),
  then a weighted mean of what the row has (`min_signals` to require more).

## Speed

A panel is computed on request. Fundamental signals for the top 1,000 at month ends since
2012 take about 15 s. Adding price signals reads daily bars for every security in the universe
(a few minutes for all signals over a decade). Ask only for what a study needs.

## Limits

- Directions are conventions from published studies; `note` flags mixed evidence.
- Sector is today's SIC code, not point in time.
- No forward returns, portfolios or costs here: that is the planned backtester `unimatrix-zero`.
- A split shortly after a security handoff is not reflected in EPS or share growth.
