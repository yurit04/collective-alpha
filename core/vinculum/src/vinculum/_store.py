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
SizeBy = Literal["market_cap", "float_market_cap"]
SIZE_COLUMNS = {"market_cap": "issuer_market_cap", "float_market_cap": "issuer_float_market_cap"}
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
    `store` / `edgar` / `finra` default to `massive_subspace_stream.Store()`,
    `edgar_cortical_node.EdgarStore()` and `finra_sensor_array.FinraStore()` and are only needed
    by `build()`, `fundamentals()`, the short-data reads and ticker resolution with by="security".
    """

    def __init__(self, root: Path | str | None = None, settings: Settings | None = None,
                 store=None, edgar=None, finra=None):
        self.settings = settings or load_settings()
        self.layout = Layout(root or self.settings.data_root)
        self._store, self._edgar, self._finra = store, edgar, finra

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

    @property
    def finra(self):
        if self._finra is None:
            from finra_sensor_array import FinraStore
            self._finra = FinraStore()
        return self._finra

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
        from ._build import scan_panels

        files = self.layout.market_cap_files()
        if not files:
            raise FileNotFoundError("market cap table is not built yet. Run: vinculum build")
        return scan_panels(files)

    def _ids(self, tickers: list[str], on: date | None) -> list[str]:
        """Security ids for tickers as of `on` (ids pass through). A ticker not trading on
        `on` (delisted: TWTR) means the security that last held it by then, as in
        `Store.bars(by="security")`."""
        res = self.store.resolve(tickers, on=on) if on else self.store.resolve(tickers)
        found = dict(zip(res["ticker"], res["security_id"], strict=False))
        missing = [t.upper() for t in tickers if not found.get(t.upper())]
        if missing:
            m = self.store.security_master(missing)
            if m.height:
                before = pl.col("valid_from") <= on if on else pl.lit(True)
                last = (m.with_columns(before.alias("__ok")).sort("__ok", "valid_from")
                        .group_by("ticker").agg(pl.col("security_id").last()))
                found.update(dict(zip(last["ticker"], last["security_id"], strict=False)))
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

    def public_float(self, tickers=None, start: Any = None, end: Any = None, backend: Backend = "polars"):
        """Public float per company and 10-K (`dei:EntityPublicFloat`, the market value held by
        non-affiliates): cik, float_date, public_float (USD as filed), available_from,
        market_cap_date, issuer_market_cap (on the float date), ratio, unit_factor,
        float_fraction (null unless plausible), status (ok, unit_fixed, no_market_cap,
        implausible). `tickers` are resolved to CIKs as of `end`; start/end filter float_date."""
        df = self._table("public_float")
        s, e = _day(start), _day(end)
        if s:
            df = df.filter(pl.col("float_date") >= s)
        if e:
            df = df.filter(pl.col("float_date") <= e)
        names = self._list(tickers)
        if names:
            ids = self._ids(names, e)
            ciks = self.cik_map(ids)["cik"].drop_nulls().unique().to_list()
            df = df.filter(pl.col("cik").is_in(ciks))
        return self._out(df, backend)

    def market_cap(self, tickers=None, start: Any = None, end: Any = None, by: Literal["ticker", "security"] = "ticker",
                   primary_only: bool = False, lazy: bool = False, backend: Backend = "polars"):
        """Daily market cap per equity security (point in time: the SEC count usable that day).

        Columns: date, security_id, ticker, type, cik, cik_status, close (raw), volume,
        dollar_volume, split_factor, shares (in that day's units), shares_end, shares_filed,
        shares_tag, shares_jump, basis_ratio, basis, market_cap, issuer_market_cap (all listed
        classes of the company, from its primary class), is_primary, implausible, float_fraction
        (the company's public float / market cap from its latest 10-K, usable the day after
        filing, for 400 days), float_date, float_market_cap, issuer_float_market_cap.
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

    def ranks(self, on: Any, types: Iterable[str] | None = ("CS", "ADRC"), by: SizeBy = "market_cap",
              backend: Backend = "polars"):
        """Companies ranked by size on a day (one row per company: its primary class).
        Columns: rank, size (large/mid/small/micro), security_id, ticker, cik, issuer_market_cap,
        issuer_float_market_cap. by="market_cap" ranks on issuer_market_cap; by="float_market_cap"
        on issuer_float_market_cap (companies without a usable public float are left out).
        `types` keeps those Massive types plus securities with no recorded type (companies
        delisted before Massive's snapshots carried one: Dell, Broadcom, SanDisk); the panel
        already holds equity only, so leaving them out would drop exactly the acquired ones."""
        if by not in SIZE_COLUMNS:
            raise ValueError(f"unknown by={by!r}; choose from {list(SIZE_COLUMNS)}")
        col = SIZE_COLUMNS[by]
        day = _day(on)
        lf = self.scan().filter((pl.col("date") == day) & pl.col("is_primary") & pl.col(col).is_not_null())
        if types:
            lf = lf.filter(pl.col("type").is_in(list(types)) | pl.col("type").is_null())
        df = (lf.select("date", "security_id", "ticker", "type", "cik", "issuer_market_cap", "issuer_float_market_cap")
              .collect().sort(col, descending=True).with_row_index("rank", offset=1))
        bucket = pl.lit("micro")
        for name, (lo, hi) in reversed(list(SIZE_BUCKETS.items())):
            cond = pl.col("rank") >= lo if hi is None else pl.col("rank").is_between(lo, hi)
            bucket = pl.when(cond).then(pl.lit(name)).otherwise(bucket)
        return self._out(df.with_columns(bucket.alias("size")), backend)

    def universe(self, on: Any, top: int | None = None, size: str | Iterable[str] | None = None,
                 types: Iterable[str] | None = ("CS", "ADRC"), rebalance: Literal["monthly", "daily"] = "monthly",
                 by: SizeBy = "market_cap", backend: Backend = "polars"):
        """Size universe on a day, survivorship free: companies chosen by size rank on the
        rank date (previous month end by default), then their primary class's row on `on` (left
        out if it did not trade that day). top=N for the N largest; size= 'large' (ranks 1-200),
        'mid' (201-1000), 'small' (1001-3000), 'micro' (3001+), or a list of them.
        by="float_market_cap" ranks on float-adjusted size instead (see `ranks`)."""
        day = _day(on)
        rd = self.rank_date(day, rebalance)
        r = self.ranks(rd, types, by=by)
        if top is not None:
            r = r.filter(pl.col("rank") <= top)
        if size is not None:
            sizes = [size] if isinstance(size, str) else list(size)
            unknown = set(sizes) - set(SIZE_BUCKETS)
            if unknown:
                raise ValueError(f"unknown size {sorted(unknown)}; choose from {list(SIZE_BUCKETS)}")
            r = r.filter(pl.col("size").is_in(sizes))
        today = self.scan().filter(pl.col("date") == day).collect()
        out = (r.select("security_id", "rank", "size", pl.col(SIZE_COLUMNS[by]).alias("rank_market_cap"))
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
        # Predecessor CIKs (same company before a reorganization) fill in where the CIK in use
        # has no usable value yet.
        preds = self._predecessor_ciks(base)
        base = base.join(preds, on=["security_id", "cik"], how="left") if preds.height else base
        pred_cols = [c for c in base.columns if c.startswith("__pc")]
        ciks = list(set(base["cik"].drop_nulls().to_list()).union(*[base[c].drop_nulls().to_list() for c in pred_cols]))
        out = base.sort("date")
        series = self.edgar.series(items, ciks, policy) if ciks else None
        currency = None
        for item in items:
            kind = CONCEPTS[item].kind
            if series is None:
                out = out.with_columns(pl.lit(None, pl.Float64).alias(item))
                continue
            s = series.filter(pl.col("item") == item)
            if kind == "per_share":
                out = self._join_per_share(out, s, item)
                continue
            if kind == "flow":
                s = s.filter(pl.col("ttm").is_not_null()).select(
                    "cik", "end", "unit", pl.col("ttm").alias("__v"), pl.col("ttm_available_from").alias("__a"))
            else:
                s = s.select("cik", "end", "unit", pl.col("value").alias("__v"), pl.col("available_from").alias("__a"))
            # A later filing about an older period must not replace a newer one.
            s = (s.sort("cik", "__a", "end").with_columns(pl.col("end").cum_max().over("cik").alias("__m"))
                 .filter(pl.col("end") >= pl.col("__m")).drop("__m").sort("__a"))
            # Cover figures measured mid-year (public float) are only filed months later: they
            # age from their filing, as in market_cap().
            age_from = "__a" if CONCEPTS[item].second_quarter else "end"
            fresh = pl.col(age_from) >= pl.col("date") - pl.duration(days=STALE_DAYS)
            out = out.join_asof(s, left_on="date", right_on="__a", by="cik", strategy="backward", check_sortedness=False)
            out = out.with_columns(pl.when(fresh).then(pl.col("__v")).otherwise(None).alias("__v"),
                                   pl.when(fresh).then(pl.col("unit")).otherwise(None).alias("unit"))
            for col in pred_cols:
                prev = s.rename({"cik": col, "__v": "__pv", "unit": "__pu", "end": "__pe", "__a": "__pa"})
                out = out.join_asof(prev, left_on="date", right_on="__pa", by=col, strategy="backward",
                                    check_sortedness=False)
                ok = (pl.col("__v").is_null() & pl.col("__pv").is_not_null()
                      & (pl.col("__pa" if age_from == "__a" else "__pe") >= pl.col("date") - pl.duration(days=STALE_DAYS)))
                pick = {"__v": "__pv", "unit": "__pu", "end": "__pe", "__a": "__pa"}
                out = out.with_columns(*[pl.when(ok).then(pl.col(p)).otherwise(pl.col(c)).alias(c)
                                         for c, p in pick.items()]).drop(*pick.values())
            out = out.with_columns(pl.col("__v").alias(item), pl.col("unit").alias(f"__u_{item}"))
            out = out.drop("end", "unit", "__v", "__a")
            if CONCEPTS[item].unit == "currency":
                currency = pl.coalesce(currency, pl.col(f"__u_{item}")) if currency is not None else pl.col(f"__u_{item}")
        out = out.with_columns((currency if currency is not None else pl.lit(None, pl.String)).alias("currency"))
        out = out.drop([c for c in out.columns if c.startswith(("__u_", "__pc", "__ps"))])
        if ratios:
            usd = pl.col("currency") == "USD"
            for name, item in RATIOS.items():
                if item in items:
                    out = out.with_columns(pl.when(usd & (pl.col("issuer_market_cap") > 0))
                                           .then(pl.col(item) / pl.col("issuer_market_cap")).otherwise(None).alias(name))
        return self._out(out.sort("date", "security_id"), backend)

    def successors(self, security_ids=None, backend: Backend = "polars"):
        """The same company continuing under a new security (kind "security": holding-company
        reorganizations, ticker moved within 7 days at a ~1:1 price) or a new CIK (kind "cik"):
        security_id, cik, predecessor_id, predecessor_cik, link_date, kind, ticker, price_ratio
        (first close of the new security / last close of the old one), evidence."""
        df = self._table("successors")
        ids = self._list(security_ids)
        return self._out(df.filter(pl.col("security_id").is_in(ids) | pl.col("predecessor_id").is_in(ids))
                         if ids else df, backend)

    def _predecessor_ciks(self, base: pl.DataFrame) -> pl.DataFrame:
        """security_id, cik, __pc1/__ps1, __pc2/__ps2: predecessor CIKs and securities of each
        (security, CIK), nearest first."""
        from ._successors import lineage

        try:
            succ = self._table("successors")
        except FileNotFoundError:
            return pl.DataFrame()
        lin = lineage(succ.filter(pl.col("security_id").is_in(base["security_id"].unique().to_list())))
        lin = lin.filter(pl.col("predecessor_cik") != pl.col("cik"))
        if lin.is_empty():
            return pl.DataFrame()
        lin = lin.with_columns(pl.col("depth").rank("ordinal").over("security_id", "cik").cast(pl.Int32).alias("depth"))
        lin = lin.with_columns(pl.col("depth").cast(pl.String))
        pc = lin.pivot(on="depth", index=["security_id", "cik"], values="predecessor_cik")
        ps = lin.pivot(on="depth", index=["security_id", "cik"], values="predecessor_id")
        depths = [c for c in pc.columns if c not in ("security_id", "cik")]
        return (pc.rename({d: f"__pc{d}" for d in depths})
                .join(ps.rename({d: f"__ps{d}" for d in depths}), on=["security_id", "cik"]))

    def _join_per_share(self, out: pl.DataFrame, rows: pl.DataFrame, item: str) -> pl.DataFrame:
        """Add one per-share item (TTM, in each security's units on `date`) to the panel; where
        the security and CIK in use have none yet, the predecessor's (in its units at the link,
        then through the successor's own splits)."""
        from ._pershare import cum_splits, factor_at, per_share_ttm
        from ._shares import split_events

        if rows.is_empty():
            return out.with_columns(pl.lit(None, pl.Float64).alias(item))
        levels = sorted(c[4:] for c in out.columns if c.startswith("__ps"))
        pairs = out.select("security_id", "cik").drop_nulls().unique()
        ids = set(pairs["security_id"].to_list()).union(*[out[f"__ps{d}"].drop_nulls().to_list() for d in levels])
        cum = (cum_splits(split_events(self.store.security_actions("splits", list(ids)))) if ids else
               pl.DataFrame(schema={"security_id": pl.String, "execution_date": pl.Date, "c": pl.Float64}))

        def ttm(pairs: pl.DataFrame) -> pl.DataFrame:
            t = per_share_ttm(rows, pairs, cum)
            # A later filing about an older period must not replace a newer one.
            return (t.sort("security_id", "available", "end")
                    .with_columns(pl.col("end").cum_max().over("security_id").alias("__m"))
                    .filter(pl.col("end") >= pl.col("__m")).drop("__m", "unit").sort("available"))

        stale = pl.duration(days=STALE_DAYS)
        out = out.join_asof(ttm(pairs), left_on="date", right_on="available", by="security_id", strategy="backward",
                            check_sortedness=False)
        out = factor_at(out, "date", cum, "__c_now")
        out = out.with_columns(pl.when(pl.col("end") >= pl.col("date") - stale)
                               .then(pl.col("w") / pl.col("__c_now")).otherwise(None).alias(item)
                               ).drop("end", "w", "available")
        for d in levels:
            ps, pc = f"__ps{d}", f"__pc{d}"
            pp = out.select(pl.col(ps).alias("security_id"), pl.col(pc).alias("cik")).drop_nulls().unique()
            if pp.is_empty():
                continue
            t = ttm(pp).rename({"security_id": ps})
            out = out.join_asof(t, left_on="date", right_on="available", by=ps, strategy="backward",
                                check_sortedness=False)
            # w / C_pred(date) is in the predecessor's units at its last split; a successor
            # security's own splits since the link (C_now) apply on top.
            out = factor_at(out.rename({ps: "__tmp", "security_id": "__own"}).rename({"__tmp": "security_id"}),
                            "date", cum, "__c_pred").rename({"security_id": ps, "__own": "security_id"})
            own_units = pl.when(pl.col(ps) == pl.col("security_id")).then(pl.lit(1.0)).otherwise(pl.col("__c_now"))
            ok = pl.col(item).is_null() & (pl.col("end") >= pl.col("date") - stale)
            out = out.with_columns(pl.when(ok).then(pl.col("w") / pl.col("__c_pred") / own_units)
                                   .otherwise(pl.col(item)).alias(item)).drop("end", "w", "available", "__c_pred")
        return out.drop("__c_now").sort("date")

    # ------------------------------------------------------------------ short data (FINRA)
    def _short_ids(self, tickers, by: str, end: date | None) -> list[str] | None:
        names = self._list(tickers)
        if not names:
            return None
        if by == "security":
            return self._ids(names, end)
        m = self.store.security_master([t.upper() if t == t.lower() else t for t in names])
        return m["security_id"].unique().to_list()

    def short_interest(self, tickers=None, start: Any = None, end: Any = None,
                       by: Literal["ticker", "security"] = "ticker",
                       date_by: Literal["published", "settlement"] = "published", backend: Backend = "polars"):
        """FINRA short interest per equity security (twice a month, from 2017-12-29).

        FINRA's symbols are mapped to securities through the security master on each settlement
        date. start/end filter on published_date (the default: when FINRA released it, the date
        to use for anything point in time) or settlement_date. by="ticker" means securities that
        ever traded under those tickers; by="security" resolves them as of `end`, as elsewhere.

        Columns: settlement_date, published_date, security_id, ticker (Massive's that day),
        symbol (FINRA's), market, type, cik, is_primary, short_interest (shares),
        change_pct, avg_daily_volume, days_to_cover (FINRA's), close (raw, on the settlement
        date), short_value (= short_interest x close), si_pct_market_cap (short_value / issuer
        market cap: shares short / shares outstanding for single-class companies),
        si_pct_float (short_value / issuer float market cap).
        """
        from ._short import short_interest

        s, e = _day(start), _day(end)
        if date_by not in ("published", "settlement"):
            raise ValueError("date_by must be 'published' or 'settlement'")
        df = short_interest(self.finra, self.store, self.scan(), s, e, date_by, self._short_ids(tickers, by, e))
        return self._out(df, backend)

    def short_volume(self, tickers=None, start: Any = None, end: Any = None,
                     by: Literal["ticker", "security"] = "ticker", backend: Backend = "polars"):
        """FINRA daily short sale volume per equity security (off-exchange trades, from 2009-08-03),
        mapped through the security master. Posted by 6 pm ET: usable from the next trading day.

        Columns: date, security_id, ticker, symbol, type, cik, short_volume, short_exempt_volume,
        off_exchange_volume (FINRA's total: TRF/ADF/ORF trades only), volume (all venues, from
        bars), short_ratio (short_volume / off_exchange_volume, typically 0.4-0.5: market makers
        sell short to fill buys), off_exchange_share (off_exchange_volume / volume), source.
        Without tickers this reads every security for the range: narrow start/end.
        """
        from ._short import short_volume

        s, e = _day(start), _day(end)
        df = short_volume(self.finra, self.store, self.scan(), s, e, self._short_ids(tickers, by, e))
        return self._out(df, backend)

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
