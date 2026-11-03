"""The daily backtest engine.

Weights formed on a signal date s trade at the close `lag` trading days later (e). Positions
then drift with daily total returns until the next execution date, where the portfolio trades
from its drifted weights to the new targets; trading costs are charged on the traded fraction
and borrow costs daily on short value. Weights are fractions of capital (NAV), so a long-short
portfolio with +1 / -1 legs has gross exposure 2.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import polars as pl
from neural_processor import predecessors, relabel, security_links, short_signals

from ._analytics import by_year, exposures, summary
from ._costs import CostModel
from ._returns import daily_returns

LOOKBACK = timedelta(days=120)   # bars before the first date (volatility, spread, ADV windows)


def _day(d: Any) -> date | None:
    if d is None:
        return None
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d))


@dataclass
class Backtest:
    """Result of Engine.run. daily: date, gross, net, long, short, borrow, cost, turnover,
    gross_exposure, net_exposure, nav. trades: date, security_id, before, after, cost.
    weights: the targets as traded (execution date `date`, signal date `signal_date`)."""

    daily: pl.DataFrame
    trades: pl.DataFrame
    weights: pl.DataFrame
    market: pl.DataFrame | None = None
    settings: dict = field(default_factory=dict)

    def summary(self) -> pl.DataFrame:
        return summary(self.daily, self.market, self.weights["date"].n_unique())

    def by_year(self) -> pl.DataFrame:
        return by_year(self.daily)

    def exposures(self) -> pl.DataFrame:
        return exposures(self.weights)


class Engine:
    """Backtests on vinculum universes and Massive daily bars."""

    def __init__(self, vinculum=None, store=None):
        self._v, self._store = vinculum, store
        self._cache: dict[tuple, tuple[set[str], pl.DataFrame]] = {}   # (start, end, delisting) -> ids, returns

    @property
    def vinculum(self):
        if self._v is None:
            from vinculum import Vinculum

            self._v = Vinculum()
        return self._v

    @property
    def store(self):
        if self._store is None:
            from massive_subspace_stream import Store

            self._store = Store()
        return self._store

    # ------------------------------------------------------------------ data
    def links(self) -> pl.DataFrame:
        try:
            return security_links(self.vinculum.successors())
        except (FileNotFoundError, AttributeError):
            return security_links(None)

    def calendar(self, start: date, end: date | None) -> list[date]:
        days = self.store.trading_days(start, end or date.today())
        days = [d if isinstance(d, date) else d.date() for d in days]
        return days

    def returns(self, ids: list[str], start: Any, end: Any = None, delisting: str = "shumway") -> pl.DataFrame:
        """Daily returns of `ids` (and of the securities they succeeded) from `start`: see
        _returns.daily_returns for the columns. The last result is cached (same start, end and
        delisting rule), so repeated runs over the same universe read the bars once."""
        s, e = _day(start), _day(end)
        key = (s, e, delisting)
        want = list(ids)
        hit = self._cache.get(key)
        if hit and set(want) <= hit[0]:
            return hit[1].filter(pl.col("security_id").is_in(want))
        if hit:  # load the union, so later calls on either set hit the cache
            ids = sorted(set(want) | hit[0])
        links = self.links()
        all_ids = predecessors(list(ids), links)
        bars = (self.store.bars(None, start=s - LOOKBACK, end=e, adjust="all", lazy=True)
                .filter(pl.col("security_id").is_in(all_ids)))
        cal = self.calendar(s - LOOKBACK, e)
        # The end of the data is the market's last bar, not these securities' (else a delisted
        # security's last day would look like the end of the data).
        recent = (e or date.today()) - timedelta(days=15)
        last = self.store.bars(None, start=recent, end=e, lazy=True).select(pl.col("date").max()).collect().item()
        cal = [d for d in cal if last is None or d <= last]
        r = daily_returns(bars, links, cal, delisting).filter(pl.col("date") >= s)
        self._cache = {key: (set(ids), r)}                                # one entry: bounded memory
        return r.filter(pl.col("security_id").is_in(want))

    def market(self, start: Any, end: Any = None, ticker: str = "SPY") -> pl.DataFrame:
        """date, ret: daily total return of a market proxy (SPY by default)."""
        b = self.store.bars([ticker], start=_day(start), end=_day(end), adjust="all")
        return b.sort("date").select("date", (pl.col("close") / pl.col("close").shift(1) - 1).alias("ret")).drop_nulls()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _exec_dates(signal_dates: list[date], cal: list[date], lag: int) -> dict[date, date]:
        out = {}
        for s in signal_dates:
            i = bisect.bisect_left(cal, s)
            j = i + lag
            if i < len(cal) and j < len(cal):
                out[s] = cal[j]
        return out

    @staticmethod
    def _filter(w: pl.DataFrame, r: pl.DataFrame, min_price: float | None, min_dollar_volume: float | None) -> pl.DataFrame:
        """Drop names below a price or dollar-volume floor on the signal date; rescale each leg
        back to its original total."""
        if not min_price and not min_dollar_volume:
            return w
        aux = r.select("security_id", "date", "raw_close", "adv").sort("date")
        j = w.sort("signal_date").join_asof(aux, left_on="signal_date", right_on="date", by="security_id",
                                            strategy="backward", tolerance="10d", suffix="_r",
                                            check_sortedness=False)
        ok = pl.lit(True)
        if min_price:
            ok = ok & (pl.col("raw_close") >= min_price)
        if min_dollar_volume:
            ok = ok & (pl.col("adv") >= min_dollar_volume)
        leg = pl.col("weight").sign().alias("__s")
        orig = w.group_by("date", leg).agg(pl.col("weight").sum().alias("__orig"))
        kept = (j.filter(ok.fill_null(False)).with_columns(leg).join(orig, on=["date", "__s"])
                .with_columns((pl.col("weight") / pl.col("weight").sum().over("date", "__s") * pl.col("__orig"))
                              .alias("weight")))
        return kept.select(w.columns)

    # ------------------------------------------------------------------ the run
    def run(self, weights: pl.DataFrame, start: Any = None, end: Any = None, lag: int = 1,
            costs: CostModel | None = None, delisting: str = "shumway", min_price: float | None = None,
            min_dollar_volume: float | None = None, market: str | None = "SPY") -> Backtest:
        """Simulate `weights` (date, security_id, weight; date = signal date)."""
        costs = costs or CostModel()
        if lag < 0:
            raise ValueError("lag must be >= 0")
        w = weights.filter(pl.col("weight") != 0)
        s, e = _day(start), _day(end)
        if s:
            w = w.filter(pl.col("date") >= s)
        if e:
            w = w.filter(pl.col("date") <= e)
        if w.is_empty():
            raise ValueError("no weights in the period")
        links = self.links()
        w = relabel(w, links)                      # a predecessor's rows count as the successor's
        w = w.group_by("date", "security_id").agg(pl.col("weight").sum(), *[pl.col(c).first() for c in w.columns
                                                                            if c not in ("date", "security_id", "weight")])
        first = w["date"].min()
        r = self.returns(w["security_id"].unique().to_list(), first, e, delisting)
        cal = sorted(r["date"].unique().to_list())
        if not cal:
            raise ValueError("no returns for these securities")
        exec_map = self._exec_dates(sorted(w["date"].unique().to_list()), cal, lag)
        w = (w.rename({"date": "signal_date"})
             .with_columns(pl.col("signal_date").replace_strict(exec_map, default=None, return_dtype=pl.Date).alias("date"))
             .filter(pl.col("date").is_not_null()))
        w = self._filter(w, r, min_price, min_dollar_volume)
        # Several signal dates on one execution date: the latest wins.
        latest = w.group_by("date").agg(pl.col("signal_date").max())
        w = w.join(latest, on=["date", "signal_date"]).sort("date", "security_id")
        execs = sorted(w["date"].unique().to_list())
        # Borrow rates for short positions.
        shorts = w.filter(pl.col("weight") < 0).select("security_id", "date").unique()
        borrow = self._borrow(shorts, costs)
        rets = r.select("security_id", "date", "ret", "delisted")
        aux = r.select("security_id", "date", "half_spread", "vol", "adv").sort("date")
        days_out, trades_out = [], []
        drift = pl.DataFrame(schema={"security_id": pl.String, "drift": pl.Float64, "cash": pl.Boolean})
        nav = 1.0
        for k, ex in enumerate(execs):
            nxt = execs[k + 1] if k + 1 < len(execs) else cal[-1]
            target = w.filter(pl.col("date") == ex).select("security_id", pl.col("weight").alias("target"))
            # ---- rebalance at ex: from drifted weights to targets
            t = target.join(drift, on="security_id", how="full", coalesce=True).with_columns(
                pl.col("target").fill_null(0), pl.col("drift").fill_null(0), pl.col("cash").fill_null(False))
            t = t.with_columns((pl.col("target") - pl.col("drift")).alias("trade"))
            t = (t.with_columns(pl.lit(ex).alias("date")).sort("date")
                 .join_asof(aux, on="date", by="security_id", strategy="backward", tolerance="10d",
                            check_sortedness=False))
            unit = costs.unit_cost(pl.col("trade"), nav)
            t = t.with_columns(pl.when(pl.col("cash") | (pl.col("trade") == 0)).then(0.0)
                               .otherwise(pl.col("trade").abs() * unit).alias("cost"))
            cost = float(t["cost"].sum())
            turnover = float(t.filter(~pl.col("cash"))["trade"].abs().sum())
            trades_out.append(t.filter(pl.col("trade") != 0).select(
                "date", "security_id", pl.col("drift").alias("before"), pl.col("target").alias("after"), "cost"))
            # ---- hold from ex to nxt
            hold = target.filter(pl.col("target") != 0)
            days = [d for d in cal if ex < d <= nxt]
            if days and hold.height:
                grid = (hold.join(pl.DataFrame({"date": days}), how="cross")
                        .join(rets, on=["security_id", "date"], how="left")
                        .with_columns(pl.col("ret").fill_null(0.0), pl.col("delisted").fill_null(False))
                        .sort("security_id", "date"))
                grid = grid.with_columns((1 + pl.col("ret")).cum_prod().over("security_id").alias("G"))
                grid = grid.with_columns(pl.col("G").shift(1).over("security_id").fill_null(1.0).alias("G0"),
                                         pl.col("delisted").cum_max().over("security_id").alias("gone"))
                if borrow is not None:
                    grid = grid.join(borrow.filter(pl.col("date") == ex).select("security_id", "rate"),
                                     on="security_id", how="left")
                else:
                    grid = grid.with_columns(pl.lit(None, pl.Float64).alias("rate"))
                grid = grid.with_columns(
                    (pl.col("target") * (pl.col("G") - pl.col("G0"))).alias("pnl"),
                    pl.when(pl.col("target") < 0).then(-pl.col("target") * pl.col("G0") * pl.col("rate").fill_null(
                        costs.borrow_bps / 1e4) / 252).otherwise(0.0).alias("bor"))
                dd = (grid.group_by("date").agg(
                    pl.col("pnl").sum(), pl.col("pnl").filter(pl.col("target") > 0).sum().alias("lpnl"),
                    pl.col("pnl").filter(pl.col("target") < 0).sum().alias("spnl"), pl.col("bor").sum(),
                    (pl.col("target") * pl.col("G")).abs().sum().alias("gexp"),
                    (pl.col("target") * pl.col("G")).sum().alias("nexp")).sort("date"))
                x = 1 + (dd["pnl"] - dd["bor"]).cum_sum()
                x0 = x.shift(1).fill_null(1.0)
                dd = dd.with_columns(x.alias("X"), x0.alias("X0"))
                dd = dd.with_columns(((pl.col("pnl") - pl.col("bor")) / pl.col("X0")).alias("gross_b"),
                                     (pl.col("pnl") / pl.col("X0")).alias("gross"),
                                     (pl.col("lpnl") / pl.col("X0")).alias("long"),
                                     (pl.col("spnl") / pl.col("X0")).alias("short"),
                                     (pl.col("bor") / pl.col("X0")).alias("borrow"),
                                     (pl.col("gexp") / pl.col("X")).alias("gross_exposure"),
                                     (pl.col("nexp") / pl.col("X")).alias("net_exposure"))
                end_x = float(dd["X"][-1])
                end = grid.filter(pl.col("date") == days[-1]).select(
                    "security_id", (pl.col("target") * pl.col("G") / end_x).alias("drift"), pl.col("gone").alias("cash"))
                # The cost of the trade at ex comes out of that day's NAV (recorded on ex).
                period = dd.select("date", "gross", "long", "short", "borrow", "gross_b", "gross_exposure",
                                   "net_exposure").with_columns(pl.lit(0.0).alias("cost"), pl.lit(0.0).alias("turnover"))
                nav_start = nav * (1 - cost)
                nav = nav_start * end_x
            else:
                period = None
                end = pl.DataFrame(schema={"security_id": pl.String, "drift": pl.Float64, "cash": pl.Boolean})
                nav_start = nav * (1 - cost)
                nav = nav_start
            days_out.append(pl.DataFrame({"date": [ex], "cost": [cost], "turnover": [turnover]}))
            if period is not None:
                days_out.append(period)
            drift = end
        daily = (pl.concat(days_out, how="diagonal").group_by("date")
                 .agg(*[pl.col(c).sum() for c in ("gross", "gross_b", "long", "short", "borrow", "cost", "turnover")],
                      *[pl.col(c).drop_nulls().last() for c in ("gross_exposure", "net_exposure")])
                 .sort("date"))
        daily = daily.with_columns(((1 + pl.col("gross_b")) * (1 - pl.col("cost")) - 1).alias("net")).drop("gross_b")
        daily = daily.with_columns((1 + pl.col("net")).cum_prod().alias("nav"),
                                   pl.col("gross_exposure").forward_fill(), pl.col("net_exposure").forward_fill())
        mk = None
        if market:
            try:
                mk = self.market(daily["date"].min(), daily["date"].max(), market)
            except Exception:  # noqa: BLE001 - a missing proxy only drops beta/alpha
                mk = None
        settings = {"lag": lag, "delisting": delisting, "costs": costs, "min_price": min_price,
                    "min_dollar_volume": min_dollar_volume}
        return Backtest(daily=daily.select("date", "gross", "net", "long", "short", "borrow", "cost", "turnover",
                                           "gross_exposure", "net_exposure", "nav"),
                        trades=pl.concat(trades_out) if trades_out else pl.DataFrame(),
                        weights=w, market=mk, settings=settings)

    def _borrow(self, shorts: pl.DataFrame, costs: CostModel) -> pl.DataFrame | None:
        """security_id, date, rate (annual) for short positions on their execution dates."""
        if shorts.is_empty() or (costs.borrow_bps == 0 and costs.htb_bps == 0):
            return None
        try:
            si = self.vinculum.short_interest(start=shorts["date"].min() - timedelta(days=60), end=shorts["date"].max())
            s = short_signals(si, shorts)
        except (FileNotFoundError, AttributeError, KeyError):
            s = shorts.with_columns(pl.lit(None, pl.Float64).alias("si_pct_float"))
        return s.select("security_id", "date", costs.borrow_rate().alias("rate"))

    # ------------------------------------------------------------------ quick screens
    def forward_returns(self, panel: pl.DataFrame, lag: int = 0, delisting: str = "shumway") -> pl.DataFrame:
        """panel + ret_fwd: total return from each date's close (+lag trading days) to the next
        panel date's (+lag), per security, following successors and delistings."""
        links = self.links()
        p = relabel(panel, links)
        dates = sorted(p["date"].unique().to_list())
        r = self.returns(p["security_id"].unique().to_list(), dates[0], None, delisting)
        cal = sorted(r["date"].unique().to_list())
        ex = self._exec_dates(dates, cal, lag)
        nxt = {d: ex.get(dates[i + 1]) if i + 1 < len(dates) else None for i, d in enumerate(dates)}
        idx = r.sort("security_id", "date").with_columns((1 + pl.col("ret")).cum_prod().over("security_id").alias("tr"))
        idx = idx.select("security_id", "date", "tr").sort("date")
        q = p.with_columns(pl.col("date").replace_strict(ex, default=None, return_dtype=pl.Date).alias("__e0"),
                           pl.col("date").replace_strict(nxt, default=None, return_dtype=pl.Date).alias("__e1"))
        a = (q.select("security_id", "date", "__e0").drop_nulls().sort("__e0")
             .join_asof(idx.rename({"date": "__d"}), left_on="__e0", right_on="__d", by="security_id",
                        strategy="backward", check_sortedness=False).select("security_id", "date", pl.col("tr").alias("__t0")))
        b = (q.select("security_id", "date", "__e1").drop_nulls().sort("__e1")
             .join_asof(idx.rename({"date": "__d"}), left_on="__e1", right_on="__d", by="security_id",
                        strategy="backward", check_sortedness=False).select("security_id", "date", pl.col("tr").alias("__t1")))
        out = (p.join(a, on=["security_id", "date"], how="left").join(b, on=["security_id", "date"], how="left")
               .with_columns((pl.col("__t1") / pl.col("__t0") - 1).alias("ret_fwd")).drop("__t0", "__t1"))
        return out

    def ic(self, panel: pl.DataFrame, cols: list[str], lag: int = 0, min_names: int = 50) -> pl.DataFrame:
        """Monthly (per panel date) rank correlation of each column, oriented by its direction,
        with the forward return: signal, periods, mean_ic, ic_std, t_stat, positive."""
        from neural_processor import SIGNALS

        f = self.forward_returns(panel.select("date", "security_id", *cols), lag)
        rows = []
        for c in cols:
            sign = SIGNALS[c].direction if c in SIGNALS else 1
            g = (f.filter(pl.col(c).is_not_null() & pl.col("ret_fwd").is_not_null())
                 .group_by("date").agg((pl.corr(pl.col(c) * sign, "ret_fwd", method="spearman")).alias("ic"),
                                       pl.len().alias("n"))
                 .filter(pl.col("n") >= min_names))
            if g.height > 1:
                m, sd = float(g["ic"].mean()), float(g["ic"].std())
                rows.append({"signal": c, "periods": g.height, "mean_ic": m, "ic_std": sd,
                             "t_stat": m / sd * g.height ** 0.5 if sd > 0 else None,
                             "positive": float((g["ic"] > 0).mean())})
        return pl.DataFrame(rows)

    def quantile_returns(self, panel: pl.DataFrame, col: str, n: int = 5, lag: int = 0) -> pl.DataFrame:
        """Equal-weighted forward return per date and quantile (n = highest, oriented)."""
        from neural_processor import SIGNALS

        sign = SIGNALS[col].direction if col in SIGNALS else 1
        f = self.forward_returns(panel.select("date", "security_id", col), lag).filter(
            pl.col(col).is_not_null() & pl.col("ret_fwd").is_not_null())
        r = (pl.col(col) * sign).rank("ordinal").over("date")
        f = f.with_columns(((r - 1) * n / pl.len().over("date")).floor().cast(pl.Int32).add(1).alias("quantile"))
        return f.group_by("date", "quantile").agg(pl.col("ret_fwd").mean().alias("ret"), pl.len().alias("names")).sort(
            "date", "quantile")
