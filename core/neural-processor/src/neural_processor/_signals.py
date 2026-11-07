"""The signal catalogue: definitions, inputs and expected direction.

Sources (how a panel computes a signal):
    fundamental  an expression over the point-in-time fundamentals panel (vinculum items,
                 issuer_market_cap, currency)
    growth       an expression over current values and the values usable a year earlier
                 (columns `<name>_lag`)
    price        computed from daily Massive bars (see _price.py)
    short        FINRA short interest as published (see _processor.py)

Rules shared by the ratio signals:
- a denominator must be positive, otherwise the signal is missing (no sign flips);
- anything over market cap or enterprise value needs a USD reporter (no FX conversion);
- missing inputs stay missing (no zero fill), except where a formula says otherwise.

`direction` is the sign the literature associates with higher future returns (+1: higher is
better, -1: lower is better). For a few signals the evidence is mixed; `note` says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

GROUPS = ("value", "profitability", "quality", "leverage", "growth", "price", "short")


@dataclass(frozen=True, eq=False)
class Signal:
    name: str
    group: str
    formula: str
    direction: int
    source: str
    items: tuple[str, ...] = ()            # EDGAR items (vinculum.fundamentals) it needs
    market: tuple[str, ...] = ()           # market-cap panel columns it needs (split_factor, shares)
    needs_usd: bool = False
    expr: pl.Expr | None = field(default=None, repr=False)
    note: str = ""


c = pl.col
mcap = c("issuer_market_cap")
usd = c("currency") == "USD"


def _ratio(num: pl.Expr, den: pl.Expr, need_usd: bool = False) -> pl.Expr:
    ok = den > 0
    return pl.when((ok & usd) if need_usd else ok).then(num / den)


def _lag(name: str) -> pl.Expr:
    return c(f"{name}_lag")


_ev = mcap + c("long_term_debt").fill_null(0) + c("short_term_borrowings").fill_null(0) - c("cash").fill_null(0)
_EV_ITEMS = ("long_term_debt", "short_term_borrowings", "cash")
_ebitda = c("operating_income") + c("depreciation_amortization")
_eps_today = c("eps_diluted") * c("split_factor")          # in today's share units
_eps_today_lag = _lag("eps_diluted") * _lag("split_factor")
_shares_today = c("shares") / c("split_factor")
_shares_today_lag = _lag("shares") / _lag("split_factor")

_DEFS: list[Signal] = [
    # ---------------------------------------------------------------- value
    Signal("earnings_yield", "value", "net income / market cap (inverse P/E)", 1, "fundamental",
           ("net_income",), needs_usd=True, expr=_ratio(c("net_income"), mcap, True)),
    Signal("book_to_market", "value", "equity / market cap", 1, "fundamental",
           ("equity",), needs_usd=True, expr=_ratio(c("equity"), mcap, True)),
    Signal("sales_to_price", "value", "revenue / market cap", 1, "fundamental",
           ("revenue",), needs_usd=True, expr=_ratio(c("revenue"), mcap, True)),
    Signal("cash_flow_yield", "value", "operating cash flow / market cap", 1, "fundamental",
           ("operating_cash_flow",), needs_usd=True, expr=_ratio(c("operating_cash_flow"), mcap, True)),
    Signal("fcf_yield", "value", "(operating cash flow - capex) / market cap", 1, "fundamental",
           ("operating_cash_flow", "capex"), needs_usd=True,
           expr=_ratio(c("operating_cash_flow") - c("capex"), mcap, True)),
    Signal("dividend_yield", "value", "dividends paid / market cap", 1, "fundamental",
           ("dividends_paid",), needs_usd=True, expr=_ratio(c("dividends_paid"), mcap, True)),
    Signal("shareholder_yield", "value", "(dividends + buybacks) / market cap; a missing one counts as 0", 1,
           "fundamental", ("dividends_paid", "buybacks"), needs_usd=True,
           expr=pl.when(c("dividends_paid").is_not_null() | c("buybacks").is_not_null()).then(
               _ratio(c("dividends_paid").fill_null(0) + c("buybacks").fill_null(0), mcap, True))),
    Signal("ebit_to_ev", "value", "operating income / EV", 1, "fundamental",
           ("operating_income", *_EV_ITEMS), needs_usd=True, expr=_ratio(c("operating_income"), _ev, True)),
    Signal("ebitda_to_ev", "value", "(operating income + D&A) / EV", 1, "fundamental",
           ("operating_income", "depreciation_amortization", *_EV_ITEMS), needs_usd=True,
           expr=_ratio(_ebitda, _ev, True)),
    Signal("sales_to_ev", "value", "revenue / EV", 1, "fundamental",
           ("revenue", *_EV_ITEMS), needs_usd=True, expr=_ratio(c("revenue"), _ev, True)),
    # ---------------------------------------------------------------- profitability
    Signal("gross_margin", "profitability", "gross profit / revenue", 1, "fundamental",
           ("gross_profit", "revenue"), expr=_ratio(c("gross_profit"), c("revenue"))),
    Signal("operating_margin", "profitability", "operating income / revenue", 1, "fundamental",
           ("operating_income", "revenue"), expr=_ratio(c("operating_income"), c("revenue"))),
    Signal("net_margin", "profitability", "net income / revenue", 1, "fundamental",
           ("net_income", "revenue"), expr=_ratio(c("net_income"), c("revenue"))),
    Signal("roe", "profitability", "net income / equity", 1, "fundamental",
           ("net_income", "equity"), expr=_ratio(c("net_income"), c("equity"))),
    Signal("roa", "profitability", "net income / assets", 1, "fundamental",
           ("net_income", "assets"), expr=_ratio(c("net_income"), c("assets"))),
    Signal("gross_profitability", "profitability", "gross profit / assets (Novy-Marx)", 1, "fundamental",
           ("gross_profit", "assets"), expr=_ratio(c("gross_profit"), c("assets"))),
    Signal("cash_roa", "profitability", "operating cash flow / assets", 1, "fundamental",
           ("operating_cash_flow", "assets"), expr=_ratio(c("operating_cash_flow"), c("assets"))),
    # ---------------------------------------------------------------- quality
    Signal("accruals", "quality", "(net income - operating cash flow) / assets (Sloan)", -1, "fundamental",
           ("net_income", "operating_cash_flow", "assets"),
           expr=_ratio(c("net_income") - c("operating_cash_flow"), c("assets"))),
    Signal("asset_turnover", "quality", "revenue / assets", 1, "fundamental",
           ("revenue", "assets"), expr=_ratio(c("revenue"), c("assets"))),
    Signal("capex_to_sales", "quality", "capex / revenue", -1, "fundamental",
           ("capex", "revenue"), expr=_ratio(c("capex"), c("revenue")),
           note="investment intensity; low investment has predicted higher returns"),
    Signal("payout_ratio", "quality", "dividends paid / net income", 1, "fundamental",
           ("dividends_paid", "net_income"), expr=_ratio(c("dividends_paid"), c("net_income")),
           note="mixed evidence"),
    Signal("sbc_to_sales", "quality", "stock compensation / revenue", -1, "fundamental",
           ("stock_comp", "revenue"), expr=_ratio(c("stock_comp"), c("revenue"))),
    Signal("goodwill_to_assets", "quality", "goodwill / assets", -1, "fundamental",
           ("goodwill", "assets"), expr=_ratio(c("goodwill"), c("assets")),
           note="acquisitive companies; mixed evidence"),
    Signal("rnd_to_sales", "quality", "R&D / revenue", 1, "fundamental",
           ("rnd", "revenue"), expr=_ratio(c("rnd"), c("revenue"))),
    Signal("sga_to_sales", "quality", "SG&A / revenue", 1, "fundamental",
           ("sga", "revenue"), expr=_ratio(c("sga"), c("revenue")),
           note="organization capital (Eisfeldt-Papanikolaou); mixed evidence"),
    Signal("tax_rate", "quality", "income tax / pre-tax income", 1, "fundamental",
           ("income_tax", "pretax_income"), expr=_ratio(c("income_tax"), c("pretax_income")),
           note="mixed evidence"),
    # ---------------------------------------------------------------- leverage
    Signal("debt_to_equity", "leverage", "long-term debt / equity", -1, "fundamental",
           ("long_term_debt", "equity"), expr=_ratio(c("long_term_debt"), c("equity"))),
    Signal("liabilities_to_assets", "leverage", "liabilities / assets", -1, "fundamental",
           ("liabilities", "assets"), expr=_ratio(c("liabilities"), c("assets"))),
    Signal("cash_to_assets", "leverage", "cash / assets", 1, "fundamental",
           ("cash", "assets"), expr=_ratio(c("cash"), c("assets")), note="mixed evidence"),
    Signal("current_ratio", "leverage", "current assets / current liabilities", 1, "fundamental",
           ("current_assets", "current_liabilities"),
           expr=_ratio(c("current_assets"), c("current_liabilities"))),
    Signal("interest_coverage", "leverage", "operating income / interest expense", 1, "fundamental",
           ("operating_income", "interest_expense"),
           expr=_ratio(c("operating_income"), c("interest_expense"))),
    # ---------------------------------------------------------------- growth (vs a year earlier)
    Signal("revenue_growth", "growth", "TTM revenue / TTM revenue a year earlier - 1", 1, "growth",
           ("revenue",), expr=_ratio(c("revenue"), _lag("revenue")) - 1),
    Signal("earnings_growth", "growth", "(TTM net income - a year earlier) / assets a year earlier", 1,
           "growth", ("net_income", "assets"),
           expr=_ratio(c("net_income") - _lag("net_income"), _lag("assets"))),
    Signal("eps_growth", "growth", "TTM diluted EPS / a year earlier - 1 (same share units)", 1, "growth",
           ("eps_diluted",), market=("split_factor",), expr=_ratio(_eps_today, _eps_today_lag) - 1),
    Signal("asset_growth", "growth", "assets / assets a year earlier - 1", -1, "growth",
           ("assets",), expr=_ratio(c("assets"), _lag("assets")) - 1),
    Signal("share_issuance", "growth", "shares outstanding / a year earlier - 1 (same share units)", -1,
           "growth", market=("split_factor", "shares"), expr=_ratio(_shares_today, _shares_today_lag) - 1),
    # ---------------------------------------------------------------- price (daily bars)
    Signal("momentum_12_1", "price", "total return from 12 months to 1 month ago", 1, "price"),
    Signal("reversal_1m", "price", "total return over the last month", -1, "price"),
    Signal("high_52w", "price", "split-adjusted close / its 52-week high", 1, "price"),
    Signal("volatility_3m", "price", "annualized std of daily total returns, last 63 trading days", -1, "price"),
    Signal("beta_12m", "price", "beta of daily total returns to SPY's, 252 days", -1, "price",
           note="betting against beta"),
    Signal("turnover", "price", "mean daily dollar volume / market cap, last 63 trading days", -1, "price"),
    Signal("illiquidity", "price", "Amihud: mean |daily return| / dollar volume x 1e6, last 63 days", 1, "price"),
    Signal("size", "price", "log issuer market cap", -1, "fundamental", expr=mcap.log()),
    # ---------------------------------------------------------------- short interest (FINRA)
    Signal("si_pct_float", "short", "short interest / float-adjusted market cap", -1, "short"),
    Signal("si_pct_market_cap", "short", "short interest x close / issuer market cap", -1, "short"),
    Signal("days_to_cover", "short", "short interest / average daily volume (FINRA)", -1, "short"),
    Signal("si_change", "short", "change in short interest since the previous report", -1, "short"),
]

SIGNALS: dict[str, Signal] = {s.name: s for s in _DEFS}
assert len(SIGNALS) == len(_DEFS), "duplicate signal name"
assert all(s.group in GROUPS for s in _DEFS)

PRICE_SIGNALS = [s.name for s in _DEFS if s.source == "price"]
SHORT_SIGNALS = [s.name for s in _DEFS if s.source == "short"]


def catalogue() -> pl.DataFrame:
    """One row per signal: name, group, formula, direction, source, items, needs_usd, note."""
    return pl.DataFrame([{"name": s.name, "group": s.group, "formula": s.formula, "direction": s.direction,
                          "source": s.source, "items": list(s.items), "needs_usd": s.needs_usd, "note": s.note}
                         for s in _DEFS])
