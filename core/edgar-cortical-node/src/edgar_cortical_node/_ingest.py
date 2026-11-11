"""Load and refresh company facts.

bulk()   download companyfacts.zip (if newer), convert every company in parallel processes,
         compact one file per CIK bucket, then swap the new facts into place.
sync()   for each day since the last sync, read the SEC daily index, re-fetch company facts
         for companies that filed financial reports, and replace their rows. A weekday whose
         index is not there (not posted yet, or the SEC refused the request) is asked for
         again on the next syncs. Runs a full bulk() instead when the last one is older than
         `bulk_refresh_days`.
"""

from __future__ import annotations

import logging
import multiprocessing
import shutil
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import polars as pl

from ._client import (
    BULK_URL,
    FACTS_URL,
    INDEX_URL,
    SUBMISSIONS_BULK_URL,
    SUBMISSIONS_URL,
    SecClient,
    SecError,
)
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
    parse_profiles_chunk,
    parse_submission,
    profiles_frame,
    quarter_of,
    write_atomic,
)
from ._store import EdgarStore

log = logging.getLogger("edgar_cortical_node")
CHUNK_MEMBERS = 400  # companies per conversion task
PROFILE_CHUNK = 5000  # submissions documents per conversion task
# A weekday without a daily index is asked for again for this many days. The SEC answers 403/404
# for a holiday, for an index it has not posted yet and when it throttles; only the first is final.
INDEX_RECHECK_DAYS = 7
# Parse workers are spawned, never forked: by the time the pool starts the parent has used
# polars' thread pool, and a forked child deadlocks in it (fork is Linux's default start method).
_SPAWN = multiprocessing.get_context("spawn")


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
        with ProcessPoolExecutor(max_workers=self.settings.parse_workers, mp_context=_SPAWN) as pool:
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
        through = built - timedelta(days=1)   # the bulk file holds every filing up to here
        state.update({"bulk_last_modified": remote, "bulk_loaded_at": now.isoformat(timespec="seconds"),
                      "sync_through": through.isoformat(), "retry_ciks": [],
                      "index_pending": [d for d in state.get("index_pending", []) if d > through.isoformat()],
                      "facts_updated_at": now.isoformat(timespec="seconds"),
                      "facts_version": int(state.get("facts_version", 0)) + 1})
        self.layout.write_state(state)
        result.done = len(members)
        result.bytes = total_bytes
        result.note = f"{comp.height:,} companies; {self.series().line()}"
        return result

    # ------------------------------------------------------------------ materialized series
    def series(self) -> JobResult:
        """Build `series.parquet` (every item, first-filed policy) and `shares.parquet` for all
        companies from the facts, stamped with the facts version they came from, so that
        `EdgarStore.series` and `.shares` read instead of rebuilding. Runs at the end of `bulk`
        and `sync`; `cortical series` runs it by hand."""
        from ._store import EdgarStore

        result = JobResult("series")
        state = self.layout.read_state()
        if not state.get("bulk_loaded_at"):
            result.note = "skipped: no bulk load yet"
            return result
        t0 = time.time()
        version = int(state.get("facts_version") or 1)      # a store from before versions: this is version 1
        series, shares = EdgarStore(settings=self.settings).build_tables()
        result.bytes = write_atomic(series, self.layout.series_file) + write_atomic(shares, self.layout.shares_file)
        state = self.layout.read_state()
        state.update({"facts_version": int(state.get("facts_version") or version), "series_facts_version": version,
                      "series_built_at": datetime.now(UTC).isoformat(timespec="seconds")})
        self.layout.write_state(state)
        result.done = series["item"].n_unique()
        result.rows = series.height
        result.note = f"{shares.height:,} share counts, {time.time() - t0:.0f}s"
        return result

    # ------------------------------------------------------------------ profiles
    def profiles(self, force: bool = False, download: bool = True) -> JobResult:
        """Company profiles and name histories from the SEC's bulk submissions.zip (~1.6 GB)."""
        result = JobResult("profiles")
        state = self.layout.read_state()
        zip_path = self.layout.submissions_zip
        if download:
            remote = self.client.last_modified(SUBMISSIONS_BULK_URL)
            if not force and state.get("profiles_loaded_at") and remote == state.get("profiles_last_modified"):
                result.note = "submissions file unchanged; nothing to do"
                return result
            if force or not zip_path.exists() or remote != state.get("profiles_zip_modified"):
                log.info("profiles: downloading submissions.zip (%s)", remote)
                self.client.download(SUBMISSIONS_BULK_URL, zip_path)
                state["profiles_zip_modified"] = remote
                self.layout.write_state(state)
        else:
            if not zip_path.exists():
                raise FileNotFoundError(f"{zip_path} is missing; run with download enabled")
            remote = state.get("profiles_zip_modified")
        with zipfile.ZipFile(zip_path) as zf:
            # CIK##########.json is the main document; -submissions-NNN.json pages only hold old filings.
            members = sorted(n for n in zf.namelist() if n.endswith(".json") and "-submissions-" not in n)
        chunks = [members[i:i + PROFILE_CHUNK] for i in range(0, len(members), PROFILE_CHUNK)]
        log.info("profiles: reading %d documents in %d chunks", len(members), len(chunks))
        profiles: list[dict] = []
        names: list[dict] = []
        with ProcessPoolExecutor(max_workers=self.settings.parse_workers, mp_context=_SPAWN) as pool:
            for n, fut in enumerate(as_completed([pool.submit(parse_profiles_chunk, str(zip_path), c)
                                                  for c in chunks]), 1):
                p, nm = fut.result()
                profiles += p
                names += nm
                if n % 20 == 0 or n == len(chunks):
                    log.info("profiles: %d/%d chunks, %s companies", n, len(chunks), f"{len(profiles):,}")
        prof, nm = profiles_frame(profiles, names)
        result.bytes = write_atomic(prof, self.layout.profiles_file) + write_atomic(nm, self.layout.names_file)
        state = self.layout.read_state()
        state.update({"profiles_last_modified": remote,
                      "profiles_loaded_at": datetime.now(UTC).isoformat(timespec="seconds")})
        self.layout.write_state(state)
        result.done, result.rows = len(members), prof.height
        result.note = f"{prof.height:,} companies with periodic reports or tickers"
        return result

    def refresh_profiles(self, ciks: list[str]) -> list[str]:
        """Re-fetch submissions for some CIKs and replace their profile and names. Returns failed CIKs."""
        if not ciks or not self.layout.profiles_file.exists():
            return []
        profiles: list[dict] = []
        names: list[dict] = []
        failed: list[str] = []
        with ThreadPoolExecutor(max_workers=self.settings.workers) as pool:
            futures = {pool.submit(self.client.get_json, SUBMISSIONS_URL.format(cik=c)): c for c in ciks}
            for fut in as_completed(futures):
                try:
                    doc = fut.result()
                    parsed = parse_submission(doc) if doc else None
                except Exception as exc:  # noqa: BLE001 - one unreadable document must not stop the rest
                    log.warning("submissions %s: %s", futures[fut], exc)
                    failed.append(futures[fut])
                    continue
                if parsed:
                    profiles.append(parsed[0])
                    names += parsed[1]
        if profiles:
            prof, nm = profiles_frame(profiles, names)
            done = prof["cik"].to_list()
            for path, new in ((self.layout.profiles_file, prof), (self.layout.names_file, nm)):
                cur = pl.read_parquet(path)
                write_atomic(pl.concat([cur.filter(~pl.col("cik").is_in(done)), new])
                             .sort("cik", *(["valid_from"] if "valid_from" in new.columns else []),
                                   nulls_last=True), path)
        return failed

    # ------------------------------------------------------------------ incremental
    def filers(self, day: date) -> list[str] | None:
        """CIKs that filed a financial report on `day`, or None when the SEC has no daily index
        for it: a weekend or holiday, an index not posted yet, or a refused request."""
        url = INDEX_URL.format(year=day.year, q=quarter_of(day), ymd=day.strftime("%Y%m%d"))
        text = self.client.get_text(url)
        if not text:
            return None
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
                    if doc is None:  # no XBRL facts for this filer
                        continue
                    df = parse_companyfacts(doc)
                    row = company_row(doc, df)
                except Exception as exc:  # noqa: BLE001 - one unreadable document must not stop the rest
                    log.warning("company facts %s: %s", cik, exc)
                    failed.append(cik)
                    continue
                frames[cik] = df
                companies.append(row)
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
        if frames:  # the facts changed: the materialized series are stale until rebuilt
            state = self.layout.read_state()
            state["facts_updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            state["facts_version"] = int(state.get("facts_version", 0)) + 1
            self.layout.write_state(state)
        return rows, failed

    def _sync_profiles(self, ciks: list[str]) -> str:
        """Profiles follow the facts: monthly bulk reload, otherwise re-fetch today's filers."""
        state = self.layout.read_state()
        if not state.get("profiles_loaded_at"):
            return ""
        loaded = datetime.fromisoformat(state["profiles_loaded_at"])
        try:
            if datetime.now(UTC) - loaded > timedelta(days=self.settings.bulk_refresh_days):
                return self.profiles().line()
            failed = self.refresh_profiles(ciks)
        except SecError as exc:
            log.warning("profiles: %s", exc)
            return "profiles refresh failed (retried next sync)"
        return f"{len(ciks) - len(failed)} profile(s) refreshed" if ciks else ""

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
        first = date.fromisoformat(state["sync_through"]) + timedelta(days=1)
        new_days = [first + timedelta(days=i) for i in range((end - first).days + 1)]
        # Weekdays an earlier sync found no index for: asked again until INDEX_RECHECK_DAYS old.
        waiting = {date.fromisoformat(d) for d in state.get("index_pending", [])}
        ciks: set[str] = set(state.get("retry_ciks", []))
        pending: list[date] = []
        for day in sorted(waiting | set(new_days)):
            found = self.filers(day)
            if found is not None:
                if day in waiting:
                    log.info("sync: the daily index for %s is there now (%d filers)", day, len(found))
                ciks.update(found)
            elif day.weekday() < 5 and (end - day).days < INDEX_RECHECK_DAYS:
                pending.append(day)
        rows, failed = self.refresh_ciks(sorted(ciks))
        profile_note = self._sync_profiles(sorted(ciks))
        state = self.layout.read_state()
        state.update({"sync_through": max(end, date.fromisoformat(state["sync_through"])).isoformat(),
                      "retry_ciks": failed, "index_pending": [d.isoformat() for d in pending],
                      "last_sync": datetime.now(UTC).isoformat(timespec="seconds")})
        self.layout.write_state(state)
        result.done = len(ciks) - len(failed)
        result.rows = rows
        result.note = f"{len(new_days)} day(s) of filings" + (f"; {profile_note}" if profile_note else "")
        if pending:
            result.note += (f"; no daily index for {', '.join(d.isoformat() for d in pending)} "
                            "(a holiday, or asked again next sync)")
        if rows or not EdgarStore(settings=self.settings).series_status()["current"]:
            result.note += f"; {self.series().line()}"
        if failed:
            result.failed = {c: "fetch failed (retried next sync)" for c in failed}
        return result
