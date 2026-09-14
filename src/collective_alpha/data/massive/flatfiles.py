"""S3 flat-file listing and resumable downloads."""

from __future__ import annotations

import datetime as dt
import logging
import os
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from collective_alpha.config import Settings
from collective_alpha.data.massive.auth import S3Credentials
from collective_alpha.storage.layout import (
    FLATFILE_PREFIX,
    date_from_flatfile_key,
    raw_flatfile_path,
)
from collective_alpha.storage.manifest import Manifest

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class S3Object:
    key: str
    size: int
    etag: str
    last_modified: dt.datetime

    @property
    def date(self) -> dt.date:
        return date_from_flatfile_key(self.key)


class FlatFileStore:
    def __init__(self, settings: Settings, creds: S3Credentials):
        self.s = settings
        self._s3 = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=creds.access_key_id,
            aws_secret_access_key=creds.secret_access_key,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 5, "mode": "standard"},
                max_pool_connections=max(8, settings.max_workers * 2),
            ),
        )

    # listing -------------------------------------------------------------
    def list_prefixes(self, prefix: str = f"{FLATFILE_PREFIX}/") -> list[str]:
        out: list[str] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.s.s3_bucket, Prefix=prefix, Delimiter="/"):
            out.extend(cp["Prefix"] for cp in page.get("CommonPrefixes", []))
        return out

    def list_objects(self, dataset: str, start: dt.date | None = None, end: dt.date | None = None) -> list[S3Object]:
        """All daily files for a dataset, optionally limited to [start, end]. Listing by year prefix
        keeps requests small and lets us skip years outside the window entirely."""
        prefix = f"{FLATFILE_PREFIX}/{dataset}/"
        years = [p.rstrip("/").split("/")[-1] for p in self.list_prefixes(prefix)]
        objs: list[S3Object] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for y in sorted(years):
            if start and int(y) < start.year:
                continue
            if end and int(y) > end.year:
                continue
            for page in paginator.paginate(Bucket=self.s.s3_bucket, Prefix=f"{prefix}{y}/"):
                for o in page.get("Contents", []):
                    if not o["Key"].endswith(".csv.gz"):
                        continue
                    obj = S3Object(o["Key"], int(o["Size"]), o["ETag"].strip('"'), o["LastModified"])
                    if start and obj.date < start:
                        continue
                    if end and obj.date > end:
                        continue
                    objs.append(obj)
        objs.sort(key=lambda o: o.key)
        return objs

    def head(self, key: str) -> S3Object | None:
        try:
            r = self._s3.head_object(Bucket=self.s.s3_bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "403"):
                return None
            raise
        return S3Object(key, int(r["ContentLength"]), r["ETag"].strip('"'), r["LastModified"])

    # download ------------------------------------------------------------
    @retry(wait=wait_exponential_jitter(initial=2, max=60), stop=stop_after_attempt(6), reraise=True)
    def download(self, obj: S3Object, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + f".part-{os.getpid()}")
        try:
            self._s3.download_file(self.s.s3_bucket, obj.key, str(tmp))
            got = tmp.stat().st_size
            if got != obj.size:
                raise OSError(f"size mismatch for {obj.key}: expected {obj.size}, got {got}")
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        return dest

    def sync(
        self,
        objs: Iterable[S3Object],
        manifest: Manifest,
        dataset: str,
        on_downloaded: Callable[[S3Object, Path], None] | None = None,
        workers: int | None = None,
    ) -> tuple[int, int]:
        """Download every object not already present (by size/etag). Returns (downloaded, skipped)."""
        todo: list[S3Object] = []
        skipped = 0
        for o in objs:
            dest = raw_flatfile_path(self.s, o.key)
            if dest.exists() and manifest.is_downloaded(o.key, o.size, o.etag):
                skipped += 1
                continue
            if dest.exists() and dest.stat().st_size == o.size:
                manifest.mark_downloaded(o.key, "flatfile", dataset, o.date.isoformat(), o.size, o.etag)
                skipped += 1
                continue
            todo.append(o)
        log.info("%s: %d files to download, %d already present", dataset, len(todo), skipped)
        done = 0
        workers = workers or self.s.max_workers

        def _one(o: S3Object) -> tuple[S3Object, Path]:
            return o, self.download(o, raw_flatfile_path(self.s, o.key))

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_one, o): o for o in todo}
            for fut in as_completed(futs):
                o = futs[fut]
                try:
                    obj, path = fut.result()
                except Exception as e:  # noqa: BLE001
                    log.error("download failed %s: %s", o.key, e)
                    manifest.mark_error(o.key, "flatfile", dataset, o.date.isoformat(), str(e))
                    continue
                manifest.mark_downloaded(obj.key, "flatfile", dataset, obj.date.isoformat(), obj.size, obj.etag)
                done += 1
                if done % 25 == 0 or done == len(todo):
                    log.info("%s: downloaded %d/%d", dataset, done, len(todo))
                if on_downloaded:
                    try:
                        on_downloaded(obj, path)
                    except Exception as e:  # noqa: BLE001
                        log.error("post-download hook failed %s: %s", obj.key, e)
                        manifest.mark_error(obj.key, "flatfile", dataset, obj.date.isoformat(), f"convert: {e}")
        return done, skipped
