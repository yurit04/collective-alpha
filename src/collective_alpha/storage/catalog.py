"""DuckDB views over the curated Parquet tree, for notebooks and research code.

from collective_alpha.storage.catalog import Catalog
cat = Catalog()
cat.sql("select * from day_aggs where ticker='AAPL' order by date").pl()
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import polars as pl

from collective_alpha.config import Settings, get_settings


class Catalog:
    def __init__(self, settings: Settings | None = None, memory_limit: str = "16GB", threads: int = 8):
        self.s = settings or get_settings()
        self.con = duckdb.connect()
        self.con.execute(f"SET memory_limit='{memory_limit}'")
        self.con.execute(f"SET threads={threads}")
        self._register_views()

    # ------------------------------------------------------------------
    def _glob(self, table: str, depth: int) -> str:
        return str(self.s.curated_dir / table / ("*/" * depth + "*.parquet"))

    def _register(self, name: str, table: str, depth: int) -> None:
        d = self.s.curated_dir / table
        if not d.exists() or not any(d.rglob("*.parquet")):
            return
        pattern = self._glob(table, depth)
        self.con.execute(
            f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{pattern}', hive_partitioning=true, union_by_name=true)"
        )

    def _register_latest_snapshot(self, name: str, table: str) -> None:
        d = self.s.curated_dir / table
        if not d.exists():
            return
        parts = sorted(p for p in d.glob("asof=*") if p.is_dir())
        if not parts:
            return
        latest = parts[-1] / "data.parquet"
        self.con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{latest}')")
        self.con.execute(
            f"CREATE OR REPLACE VIEW {name}_history AS SELECT * FROM read_parquet('{d}/asof=*/data.parquet', hive_partitioning=true, union_by_name=true)"
        )

    def _register_views(self) -> None:
        self._register("day_aggs", "day_aggs", 1)
        self._register("minute_aggs", "minute_aggs", 2)
        self._register("trades", "trades", 2)
        self._register("quotes", "quotes", 2)
        for snap in (
            "tickers",
            "tickers_pit",
            "security_master",
            "securities",
            "sec_facts",
            "ticker_details",
            "ticker_types",
            "exchanges",
            "conditions",
            "market_holidays",
            "splits",
            "dividends",
            "ipos",
            "ticker_events",
            "financials_income",
            "financials_balance",
            "financials_cashflow",
            "ratios",
            "financials",
            "float",
        ):
            self._register_latest_snapshot(snap, snap)
        for monthly in ("news", "short_interest", "short_volume"):
            self._register(monthly, monthly, 2)
        self._register("universes", "universes", 2)  # hive: name, year
        self._register("panel", "panel", 1)  # hive: year
        self._register("forward_returns", "forward_returns", 1)
        self._register("intraday", "intraday", 1)
        fdir = self.s.curated_dir / "features"
        if fdir.exists():
            for g in sorted(p for p in fdir.iterdir() if p.is_dir()):
                if any(g.rglob("*.parquet")):
                    self.con.execute(
                        f"CREATE OR REPLACE VIEW features_{g.name} AS SELECT * FROM read_parquet('{g}/*/*.parquet', hive_partitioning=true, union_by_name=true)"
                    )
        attrs = self.s.curated_dir / "security_attributes" / "data.parquet"
        if attrs.exists():
            self.con.execute(f"CREATE OR REPLACE VIEW security_attributes AS SELECT * FROM read_parquet('{attrs}')")

    # ------------------------------------------------------------------
    def sql(self, query: str) -> duckdb.DuckDBPyRelation:
        return self.con.sql(query)

    def tables(self) -> list[str]:
        return [r[0] for r in self.con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()]

    def day_panel(
        self, start: dt.date | None = None, end: dt.date | None = None, tickers: list[str] | None = None
    ) -> pl.DataFrame:
        where = ["1=1"]
        if start:
            where.append(f"date >= DATE '{start}'")
        if end:
            where.append(f"date <= DATE '{end}'")
        if tickers:
            lst = ",".join(f"'{t}'" for t in tickers)
            where.append(f"ticker IN ({lst})")
        q = f"SELECT * FROM day_aggs WHERE {' AND '.join(where)} ORDER BY ticker, date"
        return self.con.sql(q).pl()

    def scan(self, table: str) -> pl.LazyFrame:
        """Polars lazy scan of a curated table (hive partitions become columns)."""
        d: Path = self.s.curated_dir / table
        return pl.scan_parquet(str(d / "**" / "*.parquet"), hive_partitioning=True)
