"""SEC EDGAR provider: point-in-time company facts (shares outstanding, basic fundamentals) by CIK."""

from __future__ import annotations

import datetime as dt
import email.utils
import io
import json
import logging
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import polars as pl

from collective_alpha.config import Settings, get_settings
from collective_alpha.data.base import DataProvider
from collective_alpha.data.sec.client import COMPANYFACTS_ZIP, SecClient
from collective_alpha.data.sec.facts import FACT_SCHEMA, dedupe_facts, parse_companyfacts
from collective_alpha.storage import layout
from collective_alpha.storage.manifest import Manifest
from collective_alpha.storage.parquet import write_parquet_atomic

log = logging.getLogger(__name__)


class SecProvider(DataProvider):
    name = "sec"

    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.s.ensure_dirs()
        self.manifest = Manifest(self.s.manifest_path)
        self.client = SecClient(self.s.sec_user_agent, max_rps=self.s.sec_max_rps)

    @property
    def raw_dir(self) -> Path:
        return self.s.raw_dir / "sec"

    @property
    def bulk_path(self) -> Path:
        return self.raw_dir / "companyfacts.zip"

    def probe(self) -> dict[str, Any]:
        h = self.client.head(COMPANYFACTS_ZIP)
        doc = self.client.companyfacts("0000320193")
        return {
            "bulk_bytes": int(h.get("content-length", 0)),
            "bulk_last_modified": h.get("last-modified"),
            "api_ok": bool(doc and doc.get("entityName")),
        }

    # ------------------------------------------------------------------ CIK universe
    def ciks_of_interest(self) -> list[str]:
        d = layout.curated_table_dir(self.s, "securities")
        parts = sorted(p for p in d.glob("asof=*") if p.is_dir())
        if not parts:
            raise RuntimeError("run `ca master build` first")
        sec = pl.read_parquet(parts[-1] / "data.parquet")
        ciks = sec.filter(pl.col("cik").is_not_null())["cik"].unique().to_list()
        return sorted(str(c).zfill(10) for c in ciks)

    # ------------------------------------------------------------------ bulk
    def bulk_is_stale(self) -> bool:
        if not self.bulk_path.exists():
            return True
        h = self.client.head(COMPANYFACTS_ZIP)
        remote = email.utils.parsedate_to_datetime(h["last-modified"]) if h.get("last-modified") else None
        local = dt.datetime.fromtimestamp(self.bulk_path.stat().st_mtime, tz=dt.UTC)
        return bool(remote and remote > local)

    def download_bulk(self, force: bool = False) -> Path:
        if force or self.bulk_is_stale():
            log.info("downloading %s (~1.4 GB)", COMPANYFACTS_ZIP)
            self.client.download(COMPANYFACTS_ZIP, self.bulk_path)
        return self.bulk_path

    def facts_from_bulk(self, ciks: Iterable[str]) -> pl.DataFrame:
        wanted = {f"CIK{c}.json" for c in ciks}
        frames: list[pl.DataFrame] = []
        missing = set(wanted)
        with zipfile.ZipFile(self.bulk_path) as zf:
            names = set(zf.namelist())
            for n in sorted(wanted & names):
                with zf.open(n) as fh:
                    doc = json.load(io.TextIOWrapper(fh, encoding="utf-8"))
                frames.append(parse_companyfacts(doc))
                missing.discard(n)
        log.info("bulk: parsed %d of %d CIKs (%d not in archive)", len(frames), len(wanted), len(missing))
        df = pl.concat(frames, how="vertical") if frames else pl.DataFrame(schema=FACT_SCHEMA)
        return dedupe_facts(df)

    # ------------------------------------------------------------------ api
    def facts_from_api(self, ciks: Iterable[str]) -> pl.DataFrame:
        frames = []
        for c in ciks:
            doc = self.client.companyfacts(c)
            if doc:
                frames.append(parse_companyfacts(doc))
        df = pl.concat(frames, how="vertical") if frames else pl.DataFrame(schema=FACT_SCHEMA)
        return dedupe_facts(df)

    # ------------------------------------------------------------------ sync
    def sync_facts(
        self, asof: dt.date | None = None, source: str = "bulk", ciks: list[str] | None = None
    ) -> dict[str, Any]:
        """Write curated/sec_facts/asof=<date>/data.parquet for every CIK in the securities table."""
        asof = asof or dt.date.today()
        ciks = ciks or self.ciks_of_interest()
        if source == "bulk":
            self.download_bulk()
            df = self.facts_from_bulk(ciks)
        else:
            df = self.facts_from_api(ciks)
        dest = layout.curated_snapshot_path(self.s, "sec_facts", asof)
        n = write_parquet_atomic(df.with_columns(asof=pl.lit(asof)).sort(["cik", "concept", "end", "filed"]), dest)
        mkey = f"sec/facts/asof={asof:%Y-%m-%d}"
        self.manifest.mark_downloaded(
            mkey,
            "sec",
            "sec_facts",
            asof.isoformat(),
            self.bulk_path.stat().st_size if self.bulk_path.exists() else 0,
            None,
        )
        self.manifest.mark_converted(mkey, dest, n)
        return {
            "ciks_requested": len(ciks),
            "ciks_with_facts": df["cik"].n_unique(),
            "rows": n,
            "shares_rows": df.filter(pl.col("concept") == "shares_outstanding").height,
            "source": source,
        }

    def close(self) -> None:
        self.client.close()
        self.manifest.close()
