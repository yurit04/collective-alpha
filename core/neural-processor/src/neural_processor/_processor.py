"""Processor: point-in-time signal panels per security."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any, Literal

import polars as pl

from ._lineage import as_lookup, from_lookup, predecessors, relabel, security_links
from ._price import LOOKBACK_DAYS, MARKET, market_returns, price_signals
from ._sectors import sic_sector
from ._signals import GROUPS, PRICE_SIGNALS, SHORT_SIGNALS, SIGNALS
from ._transforms import transform as _transform

Backend = Literal["polars", "pandas"]
LAG_TOLERANCE = "10d"      # daily panels: the row a year earlier may be this far back
SHORT_TOLERANCE = "35d"    # FINRA reports twice a month; older than this is missing
BASE = ["date", "security_id", "ticker", "cik", "sector", "issuer_market_cap"]


def _day(d: Any) -> date | None:
    """None, a date, a datetime (its date) or an ISO string -> date. Every package has this same
    copy (tests/test_shared_helpers.py checks they agree)."""
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def resolve(signals: str | Iterable[str]) -> list[str]:
    """Signal names; a group name ("value", "price", ...) expands to its signals."""
    names = [signals] if isinstance(signals, str) else list(signals)
    out: list[str] = []
    for n in names:
        if n in GROUPS:
            out += [s for s, sig in SIGNALS.items() if sig.group == n]
        elif n in SIGNALS:
            out.append(n)
        else:
            raise ValueError(f"unknown signal or group {n!r}; groups: {GROUPS}; see neural_processor.SIGNALS")
    return list(dict.fromkeys(out))


def add_lags(df: pl.DataFrame, cols: list[str], freq: str, links: pl.DataFrame | None = None) -> pl.DataFrame:
    """`<col>_lag`: the value in the security's row a year earlier (12 month ends earlier for
    monthly panels; for daily ones the last row on or before the same day a year earlier,
    within LAG_TOLERANCE). Each lagged value was itself point in time on its own date. With
    `links`, a predecessor's rows before a security handoff count as the successor's."""
    if not cols:
        return df
    lagged = df.select("security_id", "date", *cols).rename({c: f"{c}_lag" for c in cols})
    if links is not None and links.height:
        lagged = relabel(lagged, links)
    cur = as_lookup(df, links)          # a predecessor's rows look up their history under its successor
    if freq == "1m":
        m = pl.col("date").dt.year() * 12 + pl.col("date").dt.month()
        cur = cur.with_columns((m - 12).alias("__m"))
        lagged = lagged.with_columns(m.alias("__m")).drop("date")
        return from_lookup(cur.join(lagged, on=["security_id", "__m"], how="left").drop("__m"))
    cur = cur.with_columns(pl.col("date").dt.offset_by("-1y").alias("__d")).sort("__d")
    lagged = lagged.rename({"date": "__ld"}).sort("__ld")
    return from_lookup(cur.join_asof(lagged, left_on="__d", right_on="__ld", by="security_id", strategy="backward",
                                     tolerance=LAG_TOLERANCE, check_sortedness=False).drop("__d", "__ld"))


def short_signals(si: pl.DataFrame, keys: pl.DataFrame) -> pl.DataFrame:
    """Latest published FINRA report on or before each key's date (within SHORT_TOLERANCE):
    si_pct_float, si_pct_market_cap, days_to_cover, si_change (vs the security's previous report)."""
    s = (si.sort("security_id", "settlement_date")
         .with_columns((pl.col("short_interest") / pl.col("short_interest").shift(1).over("security_id") - 1)
                       .alias("si_change"))
         .select("security_id", "published_date", "si_pct_float", "si_pct_market_cap", "days_to_cover", "si_change")
         .sort("published_date"))
    return (keys.select("security_id", "date").unique().sort("date")
            .join_asof(s, left_on="date", right_on="published_date", by="security_id", strategy="backward",
                       tolerance=SHORT_TOLERANCE, check_sortedness=False)
            .drop("published_date"))


class Processor:
    """Signals per security and date. Defaults: Vinculum(), massive_subspace_stream.Store()
    and edgar_cortical_node.EdgarStore() on their configured data roots."""

    def __init__(self, vinculum=None, store=None, edgar=None):
        self._v, self._store, self._edgar = vinculum, store, edgar

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

    @property
    def edgar(self):
        if self._edgar is None:
            from edgar_cortical_node import EdgarStore

            self._edgar = EdgarStore()
        return self._edgar

    def panel(self, signals: str | Iterable[str], start: Any = None, end: Any = None, tickers=None,
              by: Literal["ticker", "security"] = "ticker", top: int | None = None,
              size: str | Iterable[str] | None = None, universe: pl.DataFrame | None = None,
              rank_by: Literal["market_cap", "float_market_cap"] = "market_cap",
              rebalance: Literal["daily", "monthly"] = "daily", freq: Literal["1m", "1d"] = "1m", transform: Literal["zscore", "rank"] | None = None,
              winsor: float | None = None, neutralize: Iterable[str] = (), policy: Literal["first", "latest"] = "first",
              backend: Backend = "polars"):
        """Point-in-time signals: one row per security (primary class) and month end (or day).

        signals: names from SIGNALS or group names. Universe: `top` N and/or `size` buckets
        ranked by vinculum (survivorship free) on each date (rebalance="daily") or on the previous
        month end, as Vinculum.universe does (rebalance="monthly"); or `universe` (date, security_id).
        transform/winsor/neutralize ("sector", "size") apply per date. Columns: date,
        security_id, ticker, cik, sector, issuer_market_cap, [rank, size_bucket], one per signal.
        """
        names = resolve(signals)
        sigs = [SIGNALS[n] for n in names]
        s, e = _day(start), _day(end)
        growth = [g for g in sigs if g.source == "growth"]
        items = sorted({i for g in sigs for i in g.items})
        market = sorted({m for g in sigs for m in g.market})
        f_start = s - timedelta(days=380) if (s and growth) else s
        v = self.vinculum
        f = v.fundamentals(items, start=f_start, end=e, tickers=tickers, by=by, freq=freq, ratios=False,
                           policy=policy)
        if f.is_empty():
            return self._out(pl.DataFrame(schema={c: pl.String for c in BASE}).with_columns(
                [pl.lit(None, pl.Float64).alias(n) for n in names]), backend)
        if market:
            dates = f["date"].unique().to_list()
            m = (v.scan().filter(pl.col("date").is_in(dates)).select("date", "security_id", *market)
                 .collect())
            f = f.join(m, on=["date", "security_id"], how="left")
        links = security_links(self._successors()) if (growth or any(n in PRICE_SIGNALS for n in names)) else None
        if growth:
            lag_cols = sorted({i for g in growth for i in g.items} | {m for g in growth for m in g.market})
            f = add_lags(f, lag_cols, freq, links)
        if s:
            f = f.filter(pl.col("date") >= s)
        # Universe on each date.
        keep: list[str] = []
        if top is not None or size is not None:
            if rebalance not in ("daily", "monthly"):
                raise ValueError("rebalance must be 'daily' or 'monthly'")
            sizes = [size] if isinstance(size, str) else (list(size) if size is not None else None)
            dates = sorted(f["date"].unique().to_list())
            if rebalance == "daily":
                rank_on = {d: d for d in dates}
            else:   # one rank date per month: the last trading day of the month before
                month = {(d.year, d.month): v.rank_date(d, "monthly") for d in dates}
                rank_on = {d: month[(d.year, d.month)] for d in dates}
            r = (v.ranks(sorted(set(rank_on.values())), by=rank_by)
                 .select(pl.col("date").alias("rank_date"), "security_id", "rank", pl.col("size").alias("size_bucket")))
            if top is not None:
                r = r.filter(pl.col("rank") <= top)
            if sizes is not None:
                r = r.filter(pl.col("size_bucket").is_in(sizes))
            on = pl.DataFrame({"date": list(rank_on), "rank_date": list(rank_on.values())},
                              schema={"date": pl.Date, "rank_date": pl.Date})
            f = f.join(on.join(r, on="rank_date").drop("rank_date"), on=["date", "security_id"])
            keep = ["rank", "size_bucket"]
        if universe is not None:
            f = f.join(universe.select("date", "security_id").unique(), on=["date", "security_id"])
        # Sector from today's SIC code.
        ciks = f["cik"].drop_nulls().unique().to_list()
        prof = self.edgar.profiles(ciks).select("cik", "sic") if ciks else pl.DataFrame(
            schema={"cik": pl.String, "sic": pl.String})
        sec = {x: sic_sector(x) for x in prof["sic"].unique().to_list()}
        prof = prof.with_columns(pl.col("sic").replace_strict(sec, default=None, return_dtype=pl.String).alias("sector"))
        f = f.join(prof.select("cik", "sector").unique("cik"), on="cik", how="left")
        # Expressions over the panel.
        f = f.with_columns([g.expr.alias(g.name) for g in sigs if g.source in ("fundamental", "growth")])
        keys = f.select("security_id", "date")
        price = [n for n in names if n in PRICE_SIGNALS]
        if price and keys.height:
            f = f.join(self._price(keys, price, links), on=["security_id", "date"], how="left")
        short = [n for n in names if n in SHORT_SIGNALS]
        if short and keys.height:
            lo, hi = keys["date"].min(), keys["date"].max()
            si = v.short_interest(start=lo - timedelta(days=60), end=hi)
            f = f.join(short_signals(si, keys).select("security_id", "date", *short), on=["security_id", "date"],
                       how="left")
        out = f.select(*BASE, *keep, *names).sort("date", "security_id")
        if transform or winsor or neutralize:
            out = _transform(out, names, method=transform, winsor=winsor, neutralize_on=neutralize)
        return self._out(out, backend)

    def _successors(self) -> pl.DataFrame | None:
        try:
            return self.vinculum.successors()
        except (FileNotFoundError, AttributeError):
            return None

    def _price(self, keys: pl.DataFrame, names: list[str], links: pl.DataFrame | None = None) -> pl.DataFrame:
        links = links if links is not None else security_links(None)
        ids = predecessors(keys["security_id"].unique().to_list(), links)
        lo, hi = keys["date"].min(), keys["date"].max()
        bars = (self.store.bars(None, start=lo - timedelta(days=LOOKBACK_DAYS), end=hi, adjust="all", lazy=True)
                .filter(pl.col("security_id").is_in(ids)))
        caps = (self.vinculum.scan().filter(pl.col("security_id").is_in(ids)
                                            & pl.col("date").is_between(lo - timedelta(days=LOOKBACK_DAYS), hi))
                .select("security_id", "date", "market_cap"))
        market = None
        if "beta_12m" in names:
            m = self.store.bars([MARKET], start=lo - timedelta(days=LOOKBACK_DAYS), end=hi, adjust="all")
            if m.is_empty():
                raise ValueError(f"beta_12m needs {MARKET} daily bars in the Massive store")
            market = market_returns(m)
        return price_signals(bars, caps, keys, names, links, market)

    @staticmethod
    def _out(df: pl.DataFrame, backend: Backend):
        return df.to_pandas() if backend == "pandas" else df
