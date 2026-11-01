"""Load and refresh company facts.

bulk()   download companyfacts.zip (if newer), convert every company in parallel processes,
         compact one file per CIK bucket, then swap the new facts into place.
sync()   for each day since the last sync, read the SEC daily index, re-fetch company facts
         for companies that filed financial reports, and replace their rows. Runs a full
         bulk() instead when the last one is older than `bulk_refresh_days`.
"""

from __future__ import annotations

import logging
import shutil
import zipfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import polars as pl

from ._client import BULK_URL, FACTS_URL, INDEX_URL, SecClient, SecError
from ._config import Settings
from ._layout import BUCKETS, Layout, bucket_of
from ._parse import (
    COMPANY_SCHEMA,
    FACT_SCHEMA,
    FINANCIAL_FORMS,
    SORT,
    company_row,
    parse_chunk,
    parse_companyfacts,
    parse_master_index,
    quarter_of,
    write_atomic,
)

log = logging.getLogger("edgar_cortical_node")
CHUNK_MEMBERS = 400  # companies per conversion task


@dataclass
class JobResult:
    job: str
    done: int = 0
    rows: int = 0
    bytes: int = 0
    failed: dict[str, str] = field(default_factory=dict)
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.failed

    def line(self) -> str:
        parts = [f"{self.job}: {self.done} done"]
        if self.rows:
            parts.append(f"{self.rows:,} rows")
        if self.bytes:
            parts.append(f"{self.bytes / 1e9:.2f} GB")
        if self.failed:
            parts.append(f"{len(self.failed)} FAILED")
        if self.note:
            parts.append(self.note)
        return ", ".join(parts)


class Ingestor:
    def __init__(self, settings: Settings, client: Any = None):
        self.settings = settings
        self.layout = Layout(settings.data_root)
        self.layout.ensure()
        self._client = client

    @property
    def client(self) -> SecClient:
        if self._client is None:
            self._client = SecClient(self.settings.user_agent, self.settings.requests_per_second)
        return self._client

    # ------------------------------------------------------------------ bulk
    def bulk(self, force: bool = False, download: bool = True) -> JobResult:
        result = JobResult("bulk")
        state = self.layout.read_state()
        zip_path = self.layout.bulk_zip
        if download:
            remote = self.client.last_modified(BULK_URL)
            if not force and state.get("bulk_loaded_at") and remote == state.get("bulk_last_modified"):
                result.note = "bulk file unchanged; nothing to do"
                return result
            # bulk_zip_modified = the file on disk; bulk_last_modified = the one loaded. A failed
            # conversion can then be re-run without downloading 1.4 GB again.
            if force or not zip_path.exists() or remote != state.get("bulk_zip_modified"):
                log.info("bulk: downloading companyfacts.zip (%s)", remote)
                self.client.download(BULK_URL, zip_path)
                state["bulk_zip_modified"] = remote
                self.layout.write_state(state)
        else:
            if not zip_path.exists():
                raise FileNotFoundError(f"{zip_path} is missing; run with download enabled")
            remote = state.get("bulk_zip_modified")

        staging = self.layout.root / "_staging"
        shutil.rmtree(staging, ignore_errors=True)
        with zipfile.ZipFile(zip_path) as zf:
            members = sorted(n for n in zf.namelist() if n.endswith(".json"))
        chunks = [members[i:i + CHUNK_MEMBERS] for i in range(0, len(members), CHUNK_MEMBERS)]
        log.info("bulk: converting %d companies in %d chunks with %d processes", len(members), len(chunks),
                 self.settings.parse_workers)
        companies: list[dict] = []
        with ProcessPoolExecutor(max_workers=self.settings.parse_workers) as pool:
            futures = [pool.submit(parse_chunk, str(zip_path), chunk, str(staging / "chunks"), i)
                       for i, chunk in enumerate(chunks)]
            for n, fut in enumerate(as_completed(futures), 1):
                rows, comps = fut.result()
                result.rows += rows
                companies += comps
                if n % 10 == 0 or n == len(futures):
                    log.info("bulk: %d/%d chunks, %s facts", n, len(futures), f"{result.rows:,}")

        new_facts = staging / "facts"
        total_bytes = 0
        for b in range(BUCKETS):
            parts = sorted((staging / "chunks" / f"bucket={b:02d}").glob("*.parquet"))
            df = pl.read_parquet(parts) if parts else pl.DataFrame(schema=FACT_SCHEMA)
            df = df.unique(maintain_order=False).sort(SORT)
            total_bytes += write_atomic(df, self.layout.bucket_file(b, new_facts))
        now = datetime.now(UTC)
        comp = pl.DataFrame(companies, schema={k: v for k, v in COMPANY_SCHEMA.items() if k != "fetched_at"},
                            orient="row").with_columns(pl.lit(now).cast(COMPANY_SCHEMA["fetched_at"]).alias("fetched_at"))
        write_atomic(comp.sort("cik"), staging / "companies.parquet")

        # Swap into place: facts/ and companies.parquet are replaced together.
        old = self.layout.root / "_facts_old"
        shutil.rmtree(old, ignore_errors=True)
        if self.layout.facts_dir.exists():
            self.layout.facts_dir.rename(old)
        new_facts.rename(self.layout.facts_dir)
        (staging / "companies.parquet").replace(self.layout.companies_file)
        shutil.rmtree(old, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)

        built = parsedate_to_datetime(remote).date() if remote else now.date()
        state.update({"bulk_last_modified": remote, "bulk_loaded_at": now.isoformat(timespec="seconds"),
                      "sync_through": (built - timedelta(days=1)).isoformat(), "retry_ciks": []})
        self.layout.write_state(state)
        result.done = len(members)
        result.bytes = total_bytes
        result.note = f"{comp.height:,} companies"
        return result

    # ------------------------------------------------------------------ incremental
    def filers(self, day: date) -> list[str]:
        """CIKs that filed a financial report on `day` (empty on weekends and holidays)."""
        url = INDEX_URL.format(year=day.year, q=quarter_of(day), ymd=day.strftime("%Y%m%d"))
        text = self.client.get_text(url)
        if not text:
            return []
        idx = parse_master_index(text)
        if not idx.height:
            return []
        return sorted(idx.filter(pl.col("form").is_in(sorted(FINANCIAL_FORMS)))["cik"].unique().to_list())

    def refresh_ciks(self, ciks: list[str]) -> tuple[int, list[str]]:
        """Re-fetch company facts for some CIKs and replace their rows. Returns (rows, failed)."""
        if not ciks:
            return 0, []
        frames: dict[str, pl.DataFrame] = {}
        companies: list[dict] = []
        failed: list[str] = []
        with ThreadPoolExecutor(max_workers=self.settings.workers) as pool:
            futures = {pool.submit(self.client.get_json, FACTS_URL.format(cik=c)): c for c in ciks}
            for fut in as_completed(futures):
                cik = futures[fut]
                try:
                    doc = fut.result()
                except SecError as exc:
                    log.warning("company facts %s: %s", cik, exc)
                    failed.append(cik)
                    continue
                if doc is None:  # no XBRL facts for this filer
                    continue
                df = parse_companyfacts(doc)
                frames[cik] = df
                companies.append(company_row(doc, df))
        rows = 0
        by_bucket: dict[int, list[str]] = {}
        for cik in frames:
            by_bucket.setdefault(bucket_of(cik), []).append(cik)
        for b, group in sorted(by_bucket.items()):
            path = self.layout.bucket_file(b)
            current = pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=FACT_SCHEMA)
            new = pl.concat([frames[c] for c in group])
            merged = (pl.concat([current.filter(~pl.col("cik").is_in(group)), new])
                      .unique(maintain_order=False).sort(SORT))
            write_atomic(merged, path)
            rows += new.height
        if companies:
            now = datetime.now(UTC)
            upd = pl.DataFrame(companies, schema={k: v for k, v in COMPANY_SCHEMA.items() if k != "fetched_at"},
                               orient="row").with_columns(pl.lit(now).cast(COMPANY_SCHEMA["fetched_at"]).alias("fetched_at"))
            path = self.layout.companies_file
            cur = pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=COMPANY_SCHEMA)
            write_atomic(pl.concat([cur.filter(~pl.col("cik").is_in(upd["cik"].to_list())), upd]).sort("cik"), path)
        return rows, failed

    def sync(self, through: date | None = None) -> JobResult:
        state = self.layout.read_state()
        if not state.get("bulk_loaded_at"):
            return JobResult("sync", note="skipped: no bulk load yet (run `cortical backfill`)")
        loaded = datetime.fromisoformat(state["bulk_loaded_at"])
        if datetime.now(UTC) - loaded > timedelta(days=self.settings.bulk_refresh_days):
            log.info("sync: last bulk load is older than %d days; reloading", self.settings.bulk_refresh_days)
            res = self.bulk()
            res.job = "sync (bulk refresh)"
            return res
        result = JobResult("sync")
        end = through or (datetime.now(UTC).date() - timedelta(days=1))
        day = date.fromisoformat(state["sync_through"]) + timedelta(days=1)
        ciks: set[str] = set(state.get("retry_ciks", []))
        days = 0
        while day <= end:
            ciks.update(self.filers(day))
            day += timedelta(days=1)
            days += 1
        rows, failed = self.refresh_ciks(sorted(ciks))
        state = self.layout.read_state()
        state.update({"sync_through": max(end, date.fromisoformat(state["sync_through"])).isoformat(),
                      "retry_ciks": failed, "last_sync": datetime.now(UTC).isoformat(timespec="seconds")})
        self.layout.write_state(state)
        result.done = len(ciks) - len(failed)
        result.rows = rows
        result.note = f"{days} day(s) of filings"
        if failed:
            result.failed = {c: "fetch failed (retried next sync)" for c in failed}
        return result
