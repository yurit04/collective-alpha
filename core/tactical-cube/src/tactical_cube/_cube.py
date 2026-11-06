"""Cube: the intraday backtest loop, and its result."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import polars as pl
from unimatrix_zero import by_year as _by_year
from unimatrix_zero import summary as _summary

from ._aux import AuxCache
from ._costs import IntradayCosts
from ._lookahead import check_lookahead as _check
from ._sim import CARRY_SCHEMA, PENDING_SCHEMA, simulate_day

log = logging.getLogger("tactical_cube")
Strategy = Callable[[date, pl.DataFrame], pl.DataFrame | None]
DAILY_COLUMNS = ["date", "gross", "net", "long", "short", "borrow", "cost", "turnover", "gross_exposure",
                 "net_exposure", "max_gross", "overnight_gross", "fills", "rebalances", "nav"]
TARGET_COLUMNS = {"bar_end": pl.Datetime("ns", "UTC"), "security_id": pl.String, "weight": pl.Float64}


def _day(d: Any) -> date | None:
    if d is None or isinstance(d, date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    return date.fromisoformat(str(d)[:10])


def _targets(t: pl.DataFrame | None) -> pl.DataFrame:
    if t is None or t.height == 0:
        return pl.DataFrame(schema={**TARGET_COLUMNS, "auction": pl.String})
    missing = set(TARGET_COLUMNS) - set(t.columns)
    if missing:
        raise ValueError(f"targets need columns {sorted(TARGET_COLUMNS)}; missing {sorted(missing)}")
    t = t.with_columns([pl.col(c).cast(dt) for c, dt in TARGET_COLUMNS.items()])
    if "auction" not in t.columns:
        t = t.with_columns(auction=pl.lit(None, pl.String))
    t = t.select(*TARGET_COLUMNS, "auction").filter(pl.col("weight").is_not_null())
    dup = t.group_by("bar_end", "auction", "security_id").len().filter(pl.col("len") > 1)
    if dup.height:
        raise ValueError(f"{dup.height} duplicate targets (same bar_end, auction and security_id)")
    return t


@dataclass
class IntradayBacktest:
    """Result of Cube.run.

    daily     date, gross, net, long, short, borrow, cost, turnover, gross_exposure (mean over
              the day's bars), net_exposure, max_gross, overnight_gross, fills, rebalances, nav;
              returns on fixed capital (`nav` compounds them). unimatrix-zero's summary/by_year
              read it as they read a daily backtest.
    bars      date, bar, bar_end, minutes_from_open, pnl, gross_exposure, net_exposure.
    fills     one row per fill: kind (bar / open_auction / close_auction), fill and decision
              time, units, price, decision_price, slippage_bps, wait_bars, participation,
              spread/impact/commission bps, cost (fraction of capital), adv, vol.
    unfilled  orders not (fully) filled and why.
    by_security  date, security_id, ticker, pnl (gross, fraction of capital).
    """

    daily: pl.DataFrame
    bars: pl.DataFrame
    fills: pl.DataFrame
    unfilled: pl.DataFrame
    by_security: pl.DataFrame
    market: pl.DataFrame | None = None
    settings: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> pl.DataFrame:
        return _summary(self.daily, self.market, int(self.daily["rebalances"].sum()) or None)

    def by_year(self) -> pl.DataFrame:
        return _by_year(self.daily)

    def by_time_of_day(self) -> pl.DataFrame:
        """Mean gross P&L per bar (bp of capital) and its share of the total, by bar end."""
        b = self.bars
        total = float(b["pnl"].sum()) or float("nan")
        return (b.group_by("minutes_from_open")
                .agg(pl.col("bar_end").dt.convert_time_zone("America/New_York").dt.strftime("%H:%M").first().alias("ends"),
                     (pl.col("pnl").mean() * 1e4).alias("mean_bp"), (pl.col("pnl").sum() / total).alias("share"),
                     pl.col("gross_exposure").mean().alias("gross_exposure"))
                .sort("minutes_from_open"))

    def round_trips(self) -> pl.DataFrame:
        """Per security and day it traded: gross P&L, costs, net, first and last fill, minutes
        between them (a round trip when the strategy is flat at the close)."""
        f = self.fills
        if f.is_empty():
            return pl.DataFrame()
        per = f.group_by("date", "security_id").agg(
            pl.col("ticker").first(), pl.len().alias("fills"), pl.col("cost").sum(),
            pl.col("fill_time").min().alias("first_fill"), pl.col("fill_time").max().alias("last_fill"),
            (pl.col("units") * pl.col("price")).filter(pl.col("units") > 0).sum().alias("bought"))
        return (per.join(self.by_security.select("date", "security_id", "pnl"), on=["date", "security_id"], how="left")
                .with_columns(pl.col("pnl").fill_null(0.0))
                .with_columns(net=pl.col("pnl") - pl.col("cost"),
                              minutes=(pl.col("last_fill") - pl.col("first_fill")).dt.total_minutes())
                .sort("date", "security_id"))

    def fill_stats(self) -> pl.DataFrame:
        """By fill kind: count, mean costs and slippage (bp per unit traded), participation, waits."""
        f = self.fills
        if f.is_empty():
            return pl.DataFrame()
        return (f.group_by("kind").agg(
            pl.len().alias("fills"), pl.col("spread_bps").mean(), pl.col("impact_bps").mean(),
            pl.col("commission_bps").mean(), pl.col("slippage_bps").mean(),
            pl.col("participation").mean(), pl.col("participation").max().alias("max_participation"),
            pl.col("wait_bars").mean()).sort("kind"))


class Cube:
    """Intraday backtests on nanoprobe-array bars.

    probe     nanoprobe_array.Nanoprobe (bars, features, daily summaries; build its summaries first)
    vinculum  vinculum.Vinculum, only for borrow rates on shorts held overnight
    """

    def __init__(self, probe=None, vinculum=None):
        self._probe, self._vinculum = probe, vinculum

    @property
    def probe(self):
        if self._probe is None:
            from nanoprobe_array import Nanoprobe

            self._probe = Nanoprobe()
        return self._probe

    @property
    def vinculum(self):
        if self._vinculum is None:
            from vinculum import Vinculum

            self._vinculum = Vinculum(store=self.probe.store)
        return self._vinculum

    def run(self, strategy: Strategy | None = None, start: Any = None, end: Any = None, *,
            targets: pl.DataFrame | None = None, tickers: str | Iterable[str] | None = None,
            top: int | None = None, size: str | None = None, freq: str | int = "5min",
            features: Iterable[str] | str | None = None, lag: int = 1, fill: str = "vwap",
            flat_by: str | None = "close", costs: IntradayCosts | None = None, participation: float = 0.10,
            moc_cutoff: int = 10, check_lookahead: int = 2, market: str | None = "SPY",
            or_minutes: int = 30, lookback: int = 20) -> IntradayBacktest:
        """Simulate a strategy over [start, end].

        strategy  f(day, bars) -> targets for that day (bar_end, security_id, weight[, auction]),
                  called once per trading day with that day's grid bars (and `features`). Or
        targets   the same columns plus `date`, precomputed.
        tickers / top / size  the securities in `bars` (see Nanoprobe.days); with `targets`,
                  its securities are added.
        lag       bars between a decision (a bar's end) and the first bar its orders can fill in.
        fill      'vwap' (default), 'open' or 'close' of the fill bar. Rows with auction='close' /
                  'open' trade in the official closing / next opening auction instead.
        flat_by   'close' (exit in the closing auction), 'HH:MM' (exit from that bar), None (hold
                  overnight: splits and dividends through prev_close, borrow on shorts).
        costs     IntradayCosts; with costs.aum, impact, per-share commission and
                  `participation` (max share of a bar's volume per fill) apply.
        check_lookahead  days on which the strategy is re-run on cut-off bars (0: off).
        """
        if (strategy is None) == (targets is None):
            raise ValueError("pass a strategy function or a targets frame (one of them)")
        t0 = time.time()
        costs = costs or IntradayCosts()
        capital = float(costs.aum or 1.0)
        cap = participation if costs.aum else None
        ids = None if tickers is None else ([tickers] if isinstance(tickers, str) else list(tickers))
        by_date: dict[date, pl.DataFrame] = {}
        if targets is not None:
            if "date" not in targets.columns:
                raise ValueError("a targets frame needs a date column")
            tg = targets.with_columns(pl.col("date").cast(pl.Date))
            start = _day(start) or tg["date"].min()
            end = _day(end) or tg["date"].max()
            ids = sorted(set(ids or []) | set(tg["security_id"].unique().to_list()))
            by_date = {k[0]: _targets(g.drop("date")) for k, g in tg.group_by("date")}
        start, end = _day(start), _day(end) or _day(start)
        if start is None:
            raise ValueError("start is required")
        aux = AuxCache(self.probe)
        carry = pl.DataFrame(schema=CARRY_SCHEMA)
        pending = pl.DataFrame(schema=PENDING_SCHEMA)
        rows, bars_out, fills_out, unfilled_out, sec_out, carry_out, mkt = [], [], [], [], [], [], []
        warns: list[str] = []
        checked = 0
        for d, bars in self.probe.days(start, end, tickers=ids, top=top, size=size, freq=freq, fill="grid",
                                       features=features, or_minutes=or_minutes, lookback=lookback):
            a = aux.day(d)
            if market:
                m = a.filter(pl.col("ticker") == market)
                if m.height:
                    mkt.append({"date": d, "ret": m["ret"][0]})
            if strategy is not None:
                t = _targets(strategy(d, bars))
                if check_lookahead and checked < check_lookahead and t.height:
                    _check(strategy, d, bars, targets=t)
                    checked += 1
            else:
                t = by_date.get(d, _targets(None))
            if not t.height and not carry.height and not pending.height:
                continue
            r = simulate_day(d, bars, t, a, carry, pending, capital=capital, lag=lag, fill=fill, flat_by=flat_by,
                             participation=cap, moc_cutoff=moc_cutoff)
            carry, pending = r.carry, r.pending
            rows.append(r.row)
            bars_out.append(r.bars)
            info = a.select("security_id", "ticker", "adv", "vol")
            if r.fills.height:
                fills_out.append(r.fills.join(info, on="security_id", how="left"))
            if r.unfilled.height:
                unfilled_out.append(r.unfilled)
            if r.by_security.height:
                sec_out.append(r.by_security.join(info.select("security_id", "ticker"), on="security_id", how="left"))
            if carry.height:
                carry_out.append(carry.with_columns(date=pl.lit(d)))
            warns += [f"{d}: {w}" for w in r.warnings]
        if not rows:
            raise ValueError("nothing traded: no targets in the period")
        daily = pl.DataFrame(rows).sort("date")
        fills = pl.concat(fills_out, how="diagonal_relaxed") if fills_out else pl.DataFrame()
        if fills.height:
            fills = costs.apply(fills, capital).with_columns(
                slippage_bps=(pl.col("units").sign() * (pl.col("price") / pl.col("decision_price") - 1) * 1e4),
                participation=pl.when(pl.col("bar_dollar_volume") > 0)
                .then((pl.col("units") * pl.col("price")).abs() / pl.col("bar_dollar_volume")))
            per_day = fills.group_by("date").agg(pl.col("cost").sum())
            daily = daily.join(per_day, on="date", how="left").with_columns(pl.col("cost").fill_null(0.0))
        else:
            daily = daily.with_columns(cost=pl.lit(0.0))
        daily = daily.join(self._borrow(carry_out, costs, capital, warns), on="date", how="left") \
            .with_columns(pl.col("borrow").fill_null(0.0))
        daily = (daily.with_columns(net=pl.col("gross") - pl.col("cost") - pl.col("borrow"))
                 .with_columns(nav=(1 + pl.col("net")).cum_prod()).select(DAILY_COLUMNS))
        if len(warns) > 20:
            warns = warns[:20] + [f"... and {len(warns) - 20} more"]
        settings = {"freq": freq, "lag": lag, "fill": fill, "flat_by": flat_by, "costs": costs, "capital": capital,
                    "participation": cap, "moc_cutoff": moc_cutoff, "tickers": ids, "top": top, "size": size,
                    "seconds": round(time.time() - t0, 1), "lookahead_checked_days": checked}
        log.info("%s days, %s fills, %.1fs", daily.height, fills.height, settings["seconds"])
        return IntradayBacktest(
            daily=daily, bars=pl.concat(bars_out) if bars_out else pl.DataFrame(), fills=fills,
            unfilled=pl.concat(unfilled_out) if unfilled_out else pl.DataFrame(),
            by_security=pl.concat(sec_out) if sec_out else pl.DataFrame(),
            market=pl.DataFrame(mkt, schema={"date": pl.Date, "ret": pl.Float64}).drop_nulls() if mkt else None,
            settings=settings, warnings=warns)

    def _borrow(self, carry_out: list[pl.DataFrame], costs: IntradayCosts, capital: float,
                warns: list[str]) -> pl.DataFrame:
        """Borrow on shorts held overnight: short value x annual rate / 252 per night, at the
        rate from the latest short interest published on or before that day (vinculum)."""
        empty = pl.DataFrame(schema={"date": pl.Date, "borrow": pl.Float64})
        if not carry_out:
            return empty
        shorts = pl.concat(carry_out).filter(pl.col("units") < 0)
        if shorts.is_empty():
            return empty
        shorts = shorts.with_columns(value=(pl.col("units") * pl.col("last_close")).abs() / capital).sort("date")
        try:
            si = (self.vinculum.short_interest(shorts["security_id"].unique().to_list(), shorts["date"].min(),
                                               shorts["date"].max(), by="security")
                  .select("security_id", pl.col("published_date").alias("date"), "si_pct_float").sort("date"))
            shorts = shorts.join_asof(si, on="date", by="security_id", strategy="backward", tolerance="35d",
                                      check_sortedness=False)
        except Exception as e:  # noqa: BLE001 - without short interest every short pays the GC rate
            warns.append(f"short interest unavailable ({e}); borrow at the general-collateral rate")
            shorts = shorts.with_columns(si_pct_float=pl.lit(None, pl.Float64))
        return (shorts.with_columns(borrow=pl.col("value") * costs.base.borrow_rate() / 252)
                .group_by("date").agg(pl.col("borrow").sum()))
