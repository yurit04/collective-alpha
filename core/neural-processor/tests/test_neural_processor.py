"""Offline tests on small synthetic panels and fake stores."""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest
from edgar_cortical_node import CONCEPTS
from neural_processor import (
    GROUPS,
    SIGNALS,
    Processor,
    add_lags,
    catalogue,
    composite,
    neutralize,
    resolve,
    short_signals,
    sic_sector,
    transform,
)
from neural_processor._price import daily, market_returns, price_signals

D = date


# ---------------------------------------------------------------- catalogue
def test_catalogue_is_consistent():
    cat = catalogue()
    assert cat.height == len(SIGNALS) >= 45
    for s in SIGNALS.values():
        assert s.group in GROUPS and s.direction in (1, -1)
        assert set(s.items) <= set(CONCEPTS), s.name
        assert (s.expr is not None) == (s.source in ("fundamental", "growth")), s.name
    assert resolve("growth") == ["revenue_growth", "earnings_growth", "eps_growth", "asset_growth", "share_issuance"]
    assert resolve(["roe", "value"])[0] == "roe"
    with pytest.raises(ValueError):
        resolve("nonsense")
    assert sic_sector("3571") == "Business equipment" and sic_sector(None) is None and sic_sector("9999") == "Other"


# ---------------------------------------------------------------- fundamental expressions
def frame(**cols):
    n = len(next(iter(cols.values())))
    base = {"issuer_market_cap": [100.0] * n, "currency": ["USD"] * n}
    return pl.DataFrame({**base, **cols})


def ev(df, *names):
    return df.with_columns([SIGNALS[n].expr.alias(n) for n in names])


def test_ratio_rules():
    df = frame(operating_cash_flow=[30.0, 30.0, 30.0], capex=[10.0, 10.0, None], equity=[50.0, -5.0, 50.0],
               net_income=[10.0, 10.0, 10.0], currency=["USD", "USD", "EUR"])
    out = ev(df, "fcf_yield", "roe", "book_to_market")
    assert out["fcf_yield"].to_list() == [0.2, 0.2, None]        # missing capex stays missing; EUR: no USD ratio
    assert out["roe"].to_list() == [0.2, None, 0.2]             # negative equity: missing, not a sign flip
    assert out["book_to_market"].to_list() == [0.5, -0.05, None]  # market cap is the denominator
    df = frame(dividends_paid=[5.0, None, None], buybacks=[None, 5.0, None],
               long_term_debt=[20.0, None, 0.0], short_term_borrowings=[10.0, None, 0.0], cash=[30.0, None, 0.0],
               operating_income=[10.0, 10.0, 10.0], depreciation_amortization=[2.0, 2.0, None])
    out = ev(df, "shareholder_yield", "ebit_to_ev", "ebitda_to_ev")
    assert out["shareholder_yield"].to_list() == [0.05, 0.05, None]   # needs one of the two
    assert out["ebit_to_ev"].to_list() == [0.1, 0.1, 0.1]             # EV = 100 + 20 + 10 - 30
    assert out["ebitda_to_ev"].to_list() == [0.12, 0.12, None]


# ---------------------------------------------------------------- growth and lags
def test_lags_and_growth():
    months = [D(2023, 1, 31), D(2023, 2, 28), D(2024, 1, 31), D(2024, 2, 29)]
    df = pl.DataFrame({"security_id": ["S"] * 4, "date": months, "revenue": [100.0, 110.0, 120.0, 99.0],
                       "eps_diluted": [4.0, 4.0, 1.5, 1.5], "split_factor": [0.5, 0.5, 1.0, 1.0],
                       "shares": [50.0, 50.0, 102.0, 102.0], "assets": [1000.0] * 4, "net_income": [10.0, 10, 30, 30]})
    out = add_lags(df, ["revenue", "eps_diluted", "split_factor", "shares", "assets", "net_income"], "1m")
    out = ev(out, "revenue_growth", "eps_growth", "share_issuance", "earnings_growth")
    last = out.filter(pl.col("date") == D(2024, 1, 31)).row(0, named=True)
    assert last["revenue_growth"] == pytest.approx(0.2)
    # A 2:1 split in between: 4.00 per old share = 2.00 per new share; 1.50 now is -25%.
    assert last["eps_growth"] == pytest.approx(-0.25)
    assert last["share_issuance"] == pytest.approx(0.02)          # 100 new-share units -> 102
    assert last["earnings_growth"] == pytest.approx(0.02)         # (30 - 10) / 1000
    assert out.filter(pl.col("date") == D(2023, 1, 31))["revenue_growth"][0] is None
    # Daily: the row on or before the same day a year earlier, within 10 days.
    d = pl.DataFrame({"security_id": ["S"] * 3, "date": [D(2023, 3, 3), D(2024, 3, 4), D(2024, 3, 20)],
                      "revenue": [100.0, 150.0, 160.0]})
    lag = add_lags(d, ["revenue"], "1d").sort("date")["revenue_lag"].to_list()
    assert lag == [None, 100.0, None]
    # A predecessor's rows before a security handoff count as the successor's.
    h = pl.DataFrame({"security_id": ["OLD", "NEW"], "date": [D(2023, 1, 31), D(2024, 1, 31)],
                      "revenue": [100.0, 130.0]})
    links = pl.DataFrame({"predecessor_id": ["OLD"], "security_id": ["NEW"], "link_date": [D(2023, 7, 2)]})
    out = add_lags(h, ["revenue"], "1m", links)
    assert out.filter(pl.col("security_id") == "NEW")["revenue_lag"].to_list() == [100.0]
    # ... and the predecessor's own rows before the link still find their year-ago values
    # (they are filed under the successor once relabelled; ExxonMobil before July 2026).
    early = pl.DataFrame({"security_id": ["OLD", "OLD"], "date": [D(2022, 1, 31), D(2023, 1, 31)],
                          "revenue": [80.0, 100.0]})
    for freq in ("1m", "1d"):
        lag = add_lags(early, ["revenue"], freq, links).sort("date")
        assert lag["security_id"].to_list() == ["OLD", "OLD"] and lag["revenue_lag"].to_list() == [None, 80.0]


# ---------------------------------------------------------------- price signals
def weekdays(a, b):
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def bars_frame(prices: dict[str, list[float]], days, split=None, div=None):
    rows = []
    for sid, px in prices.items():
        for d, p in zip(days, px, strict=True):
            rows.append((sid, d, p, 1e6, (split or {}).get(sid, 1.0), (div or {}).get(sid, 1.0)))
    return pl.DataFrame(rows, schema={"security_id": pl.String, "date": pl.Date, "close": pl.Float64,
                                      "volume": pl.Float64, "split_factor": pl.Float64, "div_factor": pl.Float64},
                        orient="row")


def test_price_signals():
    days = weekdays(D(2022, 1, 3), D(2023, 6, 30))
    n = len(days)
    up = [100 * 1.001 ** i for i in range(n)]                                 # steady riser
    zig = [99.0 if (n - 1 - i) % 2 == 0 else 101.0 for i in range(n)]       # flat, volatile, ends low
    bars = bars_frame({"UP": up, "ZIG": zig}, days)
    caps = bars.select("security_id", "date", (pl.col("close") * 1e6).alias("market_cap"))
    on = D(2023, 6, 30)
    keys = pl.DataFrame({"security_id": ["UP", "ZIG"], "date": [on, on]})
    names = ["momentum_12_1", "reversal_1m", "high_52w", "volatility_3m", "turnover", "illiquidity", "beta_12m"]
    mkt = market_returns(bars_frame({"SPY": zig}, days))
    out = {r["security_id"]: r for r in price_signals(bars.lazy(), caps.lazy(), keys, names, market=mkt
                                                      ).iter_rows(named=True)}
    u, z = out["UP"], out["ZIG"]
    i1 = max(i for i, d in enumerate(days) if d <= D(2023, 5, 30))
    i12 = max(i for i, d in enumerate(days) if d <= D(2022, 6, 30))
    assert u["momentum_12_1"] == pytest.approx(up[i1] / up[i12] - 1)        # skips the last month
    assert u["reversal_1m"] == pytest.approx(up[-1] / up[i1] - 1)
    assert u["high_52w"] == pytest.approx(1.0) and z["high_52w"] < 1
    assert z["volatility_3m"] > 0.1 > u["volatility_3m"]
    assert u["turnover"] == pytest.approx(sum(up[-63:]) / 63 / up[-1])       # mean dollar volume / today's cap
    assert z["illiquidity"] > u["illiquidity"]
    assert math.isfinite(u["beta_12m"]) and math.isfinite(z["beta_12m"])
    with pytest.raises(ValueError, match="market"):
        price_signals(bars.lazy(), caps.lazy(), keys, ["beta_12m"])


def test_beta_does_not_depend_on_the_other_securities():
    """Beta is measured against SPY, not against the securities asked for: one name alone gets
    the same beta as in a crowd, and 1 / 2 for a copy / a 2x version of the market."""
    days = weekdays(D(2022, 1, 3), D(2023, 6, 30))
    rng = np.random.default_rng(7)
    m = rng.normal(0, 0.01, len(days))
    level = lambda r: list(100 * np.cumprod(1 + r))  # noqa: E731
    spy = level(m)
    bars = bars_frame({"COPY": spy, "TWO": level(2 * m), "BIG": level(rng.normal(0, 0.01, len(days)))}, days)
    mkt = market_returns(bars_frame({"SPY": spy}, days))
    on = days[-1]

    def beta(ids):
        keys = pl.DataFrame({"security_id": ids, "date": [on] * len(ids)})
        sub = bars.filter(pl.col("security_id").is_in(ids)).lazy()
        return dict(price_signals(sub, None, keys, ["beta_12m"], market=mkt).select("security_id", "beta_12m").iter_rows())

    crowd, alone = beta(["COPY", "TWO", "BIG"]), beta(["TWO"])
    assert crowd["COPY"] == pytest.approx(1.0) and crowd["TWO"] == pytest.approx(2.0)
    assert alone["TWO"] == pytest.approx(crowd["TWO"])


def test_price_series_continue_across_a_handoff():
    days = weekdays(D(2022, 1, 3), D(2023, 6, 30))
    cut = days.index(D(2023, 1, 3))
    px = [100 * 1.001 ** i for i in range(len(days))]
    old = bars_frame({"OLD": px[:cut]}, days[:cut], div={"OLD": 0.9})     # different adjustment bases
    new = bars_frame({"NEW": px[cut:]}, days[cut:], div={"NEW": 1.0})
    bars = pl.concat([old, new]).with_columns(pl.col("close") * pl.col("div_factor"))   # close = raw x factors
    links = pl.DataFrame({"predecessor_id": ["OLD"], "security_id": ["NEW"], "link_date": [days[cut]]})
    d = daily(bars.lazy(), None, links).collect().sort("date")
    assert d["security_id"].unique().to_list() == ["NEW"]
    assert d["ret"].drop_nulls().to_list() == pytest.approx([0.001] * (len(days) - 1))   # no jump at the link
    keys = pl.DataFrame({"security_id": ["NEW"], "date": [days[-1]]})
    m = price_signals(bars.lazy(), None, keys, ["momentum_12_1"], links)["momentum_12_1"][0]
    assert m is not None and m > 0.2                                                  # history from the old security
    # The old security's own dates before the link keep their signals, under its own id.
    before = days[cut - 1]
    keys = pl.DataFrame({"security_id": ["OLD", "NEW"], "date": [before, days[-1]]})
    names = ["reversal_1m", "volatility_3m", "high_52w", "beta_12m"]      # momentum needs 12 months before
    spy = list(100 * np.cumprod(1 + np.random.default_rng(3).normal(0, 0.01, len(days))))
    mkt = market_returns(bars_frame({"SPY": spy}, days))
    got = {r["security_id"]: r for r in price_signals(bars.lazy(), None, keys, names, links, mkt).iter_rows(named=True)}
    assert set(got) == {"OLD", "NEW"} and got["OLD"]["date"] == before
    assert all(got["OLD"][n] is not None for n in names), got["OLD"]
    alone = price_signals(old.with_columns(pl.col("close") * pl.col("div_factor")).lazy(), None,
                          keys.filter(pl.col("security_id") == "OLD"), ["reversal_1m"])["reversal_1m"][0]
    assert got["OLD"]["reversal_1m"] == pytest.approx(alone)                        # same as without the link


# ---------------------------------------------------------------- short interest
def test_short_signals_use_the_published_date():
    si = pl.DataFrame({"security_id": ["S", "S"], "settlement_date": [D(2024, 1, 12), D(2024, 1, 31)],
                       "published_date": [D(2024, 1, 24), D(2024, 2, 8)], "short_interest": [100, 150],
                       "si_pct_float": [0.1, 0.15], "si_pct_market_cap": [0.08, 0.12], "days_to_cover": [2.0, 3.0]})
    keys = pl.DataFrame({"security_id": ["S"] * 4, "date": [D(2024, 1, 23), D(2024, 1, 31), D(2024, 2, 8), D(2024, 4, 1)]})
    out = short_signals(si, keys).sort("date")
    assert out["si_pct_float"].to_list() == [None, 0.1, 0.15, None]   # not before publication; stale after 35 days
    assert out["si_change"].to_list()[2] == pytest.approx(0.5)


# ---------------------------------------------------------------- transforms
def test_transforms_and_composite():
    df = pl.DataFrame({"date": [D(2024, 1, 31)] * 4 + [D(2024, 2, 29)] * 2,
                       "sector": ["A", "A", "B", "B", "A", "B"],
                       "issuer_market_cap": [1e9, 1e10, 1e9, 1e10, 1e9, 1e9],
                       "x": [1.0, 2.0, 11.0, 12.0, 5.0, 100.0], "accruals": [0.1, -0.1, 0.0, None, 0.0, 0.0]})
    z = transform(df, ["x"], method="zscore")
    assert z.filter(pl.col("date") == D(2024, 1, 31))["x"].mean() == pytest.approx(0.0)   # per date
    r = transform(df, ["x"], method="rank")["x"].to_list()
    assert r[:4] == [0.125, 0.375, 0.625, 0.875]
    s = neutralize(df, ["x"], ["sector"])
    assert s["x"].to_list()[:4] == [-0.5, 0.5, -0.5, 0.5]                                  # sector means removed
    sz = neutralize(df, ["x"], ["sector", "size"])
    assert sz["x"].to_list()[:4] == pytest.approx([0.0] * 4)                               # all explained by size
    w = transform(df.filter(pl.col("date") == D(2024, 1, 31)), ["x"], winsor=0.25)["x"].to_list()
    assert min(w) > 1.0 and max(w) < 12.0
    c = composite(df.filter(pl.col("date") == D(2024, 1, 31)), {"accruals": 1.0}, winsor=None)["composite"].to_list()
    assert c[1] > c[2] > c[0] and c[3] is None                                             # low accruals score high
    with pytest.raises(ValueError):
        neutralize(df, ["x"], ["country"])


# ---------------------------------------------------------------- Processor with fakes
MONTHS = [D(2023, m, 28) for m in range(1, 13)] + [D(2024, m, 28) for m in range(1, 13)]


class FakeVinculum:
    """The parts of vinculum.Vinculum that Processor uses: two companies, monthly rows."""

    def __init__(self):
        rows = []
        for i, d in enumerate(MONTHS):
            rows.append(("A", "AAA", "0001", d, 1000.0 + 10 * i, 100.0 + i, 10.0, 500.0, "USD"))
            rows.append(("B", "BBB", "0002", d, 200.0, 50.0, -2.0, 400.0, "USD"))
        self.f = pl.DataFrame(rows, schema=["security_id", "ticker", "cik", "date", "issuer_market_cap", "revenue",
                                            "net_income", "assets", "currency"], orient="row")

    def fundamentals(self, items, start=None, end=None, **_):
        f = self.f.filter(pl.col("date").is_between(start or D(1900, 1, 1), end or D(2100, 1, 1)))
        return f.select("date", "security_id", "ticker", "cik", "issuer_market_cap", "currency",
                        *[c for c in items if c in f.columns],
                        *[pl.lit(None, pl.Float64).alias(c) for c in items if c not in f.columns])

    SWAP = D(2024, 3, 28)   # B is the larger company on this day only

    def ranks(self, on, by="market_cap"):
        rows = [(d, sid, (1 if (sid == "B") == (d == self.SWAP) else 2), "large") for d in on for sid in ("A", "B")]
        return pl.DataFrame(rows, schema={"date": pl.Date, "security_id": pl.String, "rank": pl.UInt32,
                                          "size": pl.String}, orient="row")

    def rank_date(self, on, rebalance="monthly"):
        return on if rebalance == "daily" else max(d for d in MONTHS if (d.year, d.month) < (on.year, on.month))

    def scan(self):
        return self.f.select("date", "security_id", pl.col("issuer_market_cap").alias("market_cap"),
                             pl.lit(1.0).alias("split_factor"), pl.lit(10.0).alias("shares")).lazy()

    def short_interest(self, start=None, end=None):
        return pl.DataFrame({"security_id": ["A"], "settlement_date": [D(2024, 12, 13)],
                             "published_date": [D(2024, 12, 24)], "short_interest": [5],
                             "si_pct_float": [0.02], "si_pct_market_cap": [0.02], "days_to_cover": [1.0]})

    def successors(self):
        raise FileNotFoundError


class FakeStore:
    def bars(self, tickers=None, start=None, end=None, adjust=None, lazy=False, **_):
        days = weekdays(D(2022, 6, 1), D(2024, 12, 31))
        if tickers == ["SPY"]:
            b = bars_frame({"SPY": [100 * (1.001 if i % 2 else 0.9995) ** i for i in range(len(days))]}, days)
        else:
            b = bars_frame({"A": [10 * 1.0005 ** i for i in range(len(days))], "B": [5.0] * len(days)}, days)
        b = b.filter(pl.col("date").is_between(start, end))
        return b.lazy() if lazy else b


class FakeEdgar:
    def profiles(self, ciks):
        return pl.DataFrame({"cik": ["0001", "0002"], "sic": ["3571", "6022"]})


def test_processor_panel():
    p = Processor(vinculum=FakeVinculum(), store=FakeStore(), edgar=FakeEdgar())
    out = p.panel(["roa", "revenue_growth", "momentum_12_1", "si_pct_float", "size"],
                  start=D(2024, 1, 1), end=D(2024, 12, 31), top=1)
    assert out.columns[:6] == ["date", "security_id", "ticker", "cik", "sector", "issuer_market_cap"]
    assert set(out.filter(pl.col("date") != FakeVinculum.SWAP)["security_id"]) == {"A"}  # universe
    assert out["date"].min() >= D(2024, 1, 1)                                                 # then start
    last = out.filter(pl.col("date") == D(2024, 12, 28)).row(0, named=True)
    assert last["sector"] == "Business equipment"
    assert last["roa"] == pytest.approx(10 / 500)
    assert last["revenue_growth"] == pytest.approx((100 + 23) / (100 + 11) - 1)
    assert last["momentum_12_1"] > 0 and last["size"] == pytest.approx(math.log(1230.0))
    assert last["si_pct_float"] == 0.02
    top = lambda **kw: dict(p.panel(["roa"], start=D(2024, 2, 1), end=D(2024, 5, 31), top=1, **kw)  # noqa: E731
                            .select("date", "security_id").iter_rows())
    assert top() == {D(2024, 2, 28): "A", D(2024, 3, 28): "B", D(2024, 4, 28): "A", D(2024, 5, 28): "A"}
    assert top(rebalance="monthly") == {D(2024, 2, 28): "A", D(2024, 3, 28): "A", D(2024, 4, 28): "B",
                                        D(2024, 5, 28): "A"}                 # ranked on the previous month end
    z = p.panel(["roa", "size"], start=D(2024, 1, 1), end=D(2024, 1, 31), transform="zscore")
    assert z["roa"].to_list() == pytest.approx([1 / math.sqrt(2), -1 / math.sqrt(2)])
