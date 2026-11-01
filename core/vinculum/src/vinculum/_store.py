"""Vinculum: reads of the built tables, size universes and point-in-time fundamentals."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import polars as pl

from ._config import Settings, load_settings
from ._layout import Layout

Backend = Literal["polars", "pandas"]
STALE_DAYS = 400
# Russell-style size buckets by issuer market-cap rank (1 = largest).
SIZE_BUCKETS = {"large": (1, 200), "mid": (201, 1000), "small": (1001, 3000), "micro": (3001, None)}
RATIOS = {"earnings_yield": "net_income", "book_to_market": "equity", "sales_to_price": "revenue",
          "cash_flow_yield": "operating_cash_flow"}


def _day(d: Any) -> date | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if hasattr(d, "date"):  # pandas Timestamp
        return d.date()
    return date.fromisoformat(str(d)[:10])


class Vinculum:
    """Security-level views that need both Massive and EDGAR.

    Build the tables first (`vinculum build`, nightly via `vinculum service install`).
    `store` / `edgar` default to `massive_subspace_stream.Store()` and
    `edgar_cortical_node.EdgarStore()` and are only needed by `build()`, `fundamentals()` and
    ticker resolution with by="security".
    """

    def __init__(self, root: Path | str | None = None, settings: Settings | None = None,
                 store=None, edgar=None):
        self.settings = settings or load_settings()
        self.layout = Layout(root or self.settings.data_root)
        self._store, self._edgar = store, edgar

    def __repr__(self) -> str:
        return f"Vinculum(root={str(self.layout.root)!r})"

    # ------------------------------------------------------------------ helpers
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

    @staticmethod
    def _out(df: pl.DataFrame, backend: Backend):
        return df.to_pandas() if backend == "pandas" else df

    @staticmethod
    def _list(x: str | Iterable[str] | None) -> list[str] | None:
        if x is None:
            return None
        if isinstance(x, str):
            x = x.split(",")
        return [t.strip() for t in x if t.strip()]

    def _table(self, name: str) -> pl.DataFrame:
        path = self.layout.file(name)
        if not path.exists():
            raise FileNotFoundError(f"{name} is not built yet. Run: vinculum build")
        return pl.read_parquet(path)

    def scan(self) -> pl.LazyFrame:
        """Lazy frame over the whole market-cap table (one row per equity security and day)."""
        files = self.layout.market_cap_files()
        if not files:
            raise FileNotFoundError("market cap table is not built yet. Run: vinculum build")
        return pl.scan_parquet(files)

    def _ids(self, tickers: list[str], on: date | None) -> list[str]:
        """Security ids for tickers as of `on` (ids pass through)."""
        res = self.store.resolve(tickers, on=on) if on else self.store.resolve(tickers)
        found = dict(zip(res["ticker"], res["security_id"], strict=False))
        return [found.get(t.upper()) or t for t in tickers]

    # ------------------------------------------------------------------ built tables
    def build(self, years: list[int] | None = None) -> dict[str, Any]:
        """Rebuild every table from the Massive and EDGAR stores (about half a minute)."""
        from ._build import build
        return build(self.store, self.edgar, self.layout, self.settings.start_year, years)

    def cik_map(self, security_ids=None, backend: Backend = "polars"):
        """CIK per security and validity window: security_id, valid_from, valid_to, cik, status
        (verified, ticker, unverified, filled, repaired, override, rejected, missing),
        massive_cik, massive_name, edgar_name, similarity."""
        df = self._table("cik_map")
        ids = self._list(security_ids)
        return self._out(df.filter(pl.col("security_id").is_in(ids)) if ids else df, backend)

    def share_counts(self, security_ids=None, backend: Backend = "polars"):
        """SEC share counts per security: as reported (`shares`) and in today's units of the
        security (`shares_now`), with end, filed, available_from, tag, basis_date, jump, unit_fixed."""
        df = self._table("share_counts")
        ids = self._list(security_ids)
        return self._out(df.filter(pl.col("security_id").is_in(ids)) if ids else df, backend)

    def basis(self, security_ids=None, backend: Backend = "polars"):
        """Basis ratio per security (traded units per unit of the SEC's count) and how it was set."""
        df = self._table("basis")
        ids = self._list(security_ids)
        return self._out(df.filter(pl.col("security_id").is_in(ids)) if ids else df, backend)

    def market_cap(self, tickers=None, start: Any = None, end: Any = None, by: Literal["ticker", "security"] = "ticker",
                   primary_only: bool = False, lazy: bool = False, backend: Backend = "polars"):
        """Daily market cap per equity security (point in time: the SEC count usable that day).

        Columns: date, security_id, ticker, type, cik, cik_status, close (raw), volume,
        dollar_volume, split_factor, shares (in that day's units), shares_end, shares_filed,
        shares_tag, shares_jump, basis_ratio, basis, market_cap, issuer_market_cap (all listed
        classes of the company, from its primary class), is_primary, implausible.
        by='security' follows companies across renames (tickers resolved as of `end`).
        """
        lf = self.scan()
        s, e = _day(start), _day(end)
        if s:
            lf = lf.filter(pl.col("date") >= s)
        if e:
            lf = lf.filter(pl.col("date") <= e)
        names = self._list(tickers)
        if names:
            if by == "security":
                lf = lf.filter(pl.col("security_id").is_in(self._ids(names, e)))
            else:
                lf = lf.filter(pl.col("ticker").is_in([t.upper() for t in names]))
        if primary_only:
            lf = lf.filter(pl.col("is_primary"))
        lf = lf.sort("date", "security_id")
        if lazy:
            return lf
        return self._out(lf.collect(), backend)

    # ------------------------------------------------------------------ size
    def _trading_dates(self, start: date, end: date) -> list[date]:
        return (self.scan().filter(pl.col("date").is_between(start, end)).select("date").unique()
                .collect()["date"].sort().to_list())

    def rank_date(self, on: Any, rebalance: Literal["monthly", "daily"] = "monthly") -> date:
        """The day whose ranking applies on `on`: the last trading day of the previous month
        (monthly, like an index reconstitution), or `on` itself (daily)."""
        day = _day(on)
        if rebalance == "daily":
            return day
        first = day.replace(day=1)
        dates = self._trading_dates(first - timedelta(days=40), first - timedelta(days=1))
        if not dates:
            raise ValueError(f"no market-cap data before {first}")
        return dates[-1]

    def ranks(self, on: Any, types: Iterable[str] | None = ("CS", "ADRC"), backend: Backend = "polars"):
        """Companies ranked by market cap on a day (one row per company: its primary class).
        Columns: rank, size (large/mid/small/micro), security_id, ticker, cik, issuer_market_cap."""
        day = _day(on)
        lf = self.scan().filter((pl.col("date") == day) & pl.col("is_primary") & pl.col("issuer_market_cap").is_not_null())
        if types:
            lf = lf.filter(pl.col("type").is_in(list(types)))
        df = (lf.select("date", "security_id", "ticker", "type", "cik", "issuer_market_cap").collect()
              .sort("issuer_market_cap", descending=True).with_row_index("rank", offset=1))
        bucket = pl.lit("micro")
        for name, (lo, hi) in reversed(list(SIZE_BUCKETS.items())):
            cond = pl.col("rank") >= lo if hi is None else pl.col("rank").is_between(lo, hi)
            bucket = pl.when(cond).then(pl.lit(name)).otherwise(bucket)
        return self._out(df.with_columns(bucket.alias("size")), backend)

    def universe(self, on: Any, top: int | None = None, size: str | Iterable[str] | None = None,
                 types: Iterable[str] | None = ("CS", "ADRC"), rebalance: Literal["monthly", "daily"] = "monthly",
                 backend: Backend = "polars"):
        """Size universe on a day, survivorship free: companies chosen by market-cap rank on the
        rank date (previous month end by default), then their primary class's row on `on` (left
        out if it did not trade that day). top=N for the N largest; size= 'large' (ranks 1-200),
        'mid' (201-1000), 'small' (1001-3000), 'micro' (3001+), or a list of them."""
        day = _day(on)
        rd = self.rank_date(day, rebalance)
        r = self.ranks(rd, types)
        if top is not None:
            r = r.filter(pl.col("rank") <= top)
        if size is not None:
            sizes = [size] if isinstance(size, str) else list(size)
            unknown = set(sizes) - set(SIZE_BUCKETS)
            if unknown:
                raise ValueError(f"unknown size {sorted(unknown)}; choose from {list(SIZE_BUCKETS)}")
            r = r.filter(pl.col("size").is_in(sizes))
        today = self.scan().filter(pl.col("date") == day).collect()
        out = (r.select("security_id", "rank", "size", pl.col("issuer_market_cap").alias("rank_market_cap"))
               .join(today, on="security_id").with_columns(pl.lit(rd).alias("rank_date")).sort("rank"))
        return self._out(out, backend)

    # ------------------------------------------------------------------ fundamentals
    def fundamentals(self, items: Iterable[str] = ("revenue", "net_income", "equity"), start: Any = None,
                     end: Any = None, tickers=None, by: Literal["ticker", "security"] = "ticker",
                     freq: Literal["1d", "1m"] = "1m", primary_only: bool = True, ratios: bool = True,
                     policy: Literal["first", "latest"] = "first", backend: Backend = "polars"):
        """Point-in-time panel of EDGAR items per security and day (or month end).

        Flows (revenue, net_income, operating_cash_flow, ...) are trailing twelve months,
        usable once the last quarter was filed; instants (assets, equity, ...) are the latest
        balance sheet; values whose period ended more than 400 days earlier are missing.
        `currency` is the reporting currency. With ratios=True and USD reporters: earnings_yield,
        book_to_market, sales_to_price, cash_flow_yield (items over issuer market cap).
        """
        from edgar_cortical_node import CONCEPTS

        items = list(items)
        unknown = [i for i in items if i not in CONCEPTS]
        if unknown:
            raise ValueError(f"unknown item(s) {unknown}; choose from {sorted(CONCEPTS)}")
        base = self.market_cap(tickers, start, end, by=by, primary_only=primary_only, lazy=True)
        if freq == "1m":
            month = pl.col("date").dt.strftime("%Y-%m")
            last = base.select("date").unique().with_columns(month.alias("__m")).group_by("__m").agg(pl.col("date").max())
            base = base.join(last.select("date"), on="date")
        base = base.select("date", "security_id", "ticker", "cik", "close", "market_cap", "issuer_market_cap").collect()
        ciks = base["cik"].drop_nulls().unique().to_list()
        out = base.sort("date")
        series = self.edgar.series(items, ciks, policy) if ciks else None
        currency = None
        for item in items:
            kind = CONCEPTS[item].kind
            if series is None:
                out = out.with_columns(pl.lit(None, pl.Float64).alias(item))
                continue
            s = series.filter(pl.col("item") == item)
            if kind == "flow":
                s = s.filter(pl.col("ttm").is_not_null()).select(
                    "cik", "end", "unit", pl.col("ttm").alias("__v"), pl.col("ttm_available_from").alias("__a"))
            else:
                s = s.select("cik", "end", "unit", pl.col("value").alias("__v"), pl.col("available_from").alias("__a"))
            # A later filing about an older period must not replace a newer one.
            s = (s.sort("cik", "__a", "end").with_columns(pl.col("end").cum_max().over("cik").alias("__m"))
                 .filter(pl.col("end") >= pl.col("__m")).drop("__m").sort("__a"))
            out = out.join_asof(s, left_on="date", right_on="__a", by="cik", strategy="backward", check_sortedness=False)
            fresh = pl.col("end") >= pl.col("date") - pl.duration(days=STALE_DAYS)
            out = out.with_columns(pl.when(fresh).then(pl.col("__v")).otherwise(None).alias(item),
                                   pl.when(fresh).then(pl.col("unit")).otherwise(None).alias(f"__u_{item}"))
            out = out.drop("end", "unit", "__v", "__a")
            if CONCEPTS[item].unit == "currency":
                currency = pl.coalesce(currency, pl.col(f"__u_{item}")) if currency is not None else pl.col(f"__u_{item}")
        out = out.with_columns((currency if currency is not None else pl.lit(None, pl.String)).alias("currency"))
        out = out.drop([c for c in out.columns if c.startswith("__u_")])
        if ratios:
            usd = pl.col("currency") == "USD"
            for name, item in RATIOS.items():
                if item in items:
                    out = out.with_columns(pl.when(usd & (pl.col("issuer_market_cap") > 0))
                                           .then(pl.col(item) / pl.col("issuer_market_cap")).otherwise(None).alias(name))
        return self._out(out.sort("date", "security_id"), backend)

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        info = self.layout.read_build()
        files = self.layout.market_cap_files()
        info["market_cap_bytes"] = sum(f.stat().st_size for f in files)
        info["root"] = str(self.layout.root)
        return info

    def check(self) -> list[tuple[str, bool, str]]:
        """Regression and sanity checks on the built tables: (name, passed, detail)."""
        from ._checks import run_checks
        return run_checks(self)
