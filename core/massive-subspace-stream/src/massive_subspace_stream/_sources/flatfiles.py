"""Massive flat files: daily gzipped CSVs in an S3-compatible bucket.

Key layout: us_stocks_sip/<dataset>_v1/YYYY/MM/YYYY-MM-DD.csv.gz
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

PREFIX = "us_stocks_sip"
THROTTLE_CODES = {"429", "SlowDown", "Throttling", "TooManyRequests", "RequestLimitExceeded", "503"}


@dataclass(frozen=True)
class RemoteFile:
    day: date
    key: str
    size: int


class FlatFileClient:
    def __init__(self, endpoint: str, bucket: str, access_key_id: str, secret_access_key: str, max_pool: int = 16):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self._s3 = boto3.session.Session().client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(
                signature_version="s3v4",
                max_pool_connections=max_pool,
                retries={"max_attempts": 10, "mode": "adaptive"},
            ),
        )

    def list_files(self, s3_dir: str, start: date, end: date) -> list[RemoteFile]:
        """All daily files for `s3_dir` (e.g. 'minute_aggs_v1') with start <= day <= end."""
        out: list[RemoteFile] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for year in range(start.year, end.year + 1):
            for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{PREFIX}/{s3_dir}/{year}/"):
                for obj in page.get("Contents", []):
                    name = obj["Key"].rsplit("/", 1)[-1]
                    try:
                        day = date.fromisoformat(name[:10])
                    except ValueError:
                        continue
                    if start <= day <= end:
                        out.append(RemoteFile(day, obj["Key"], obj["Size"]))
        return sorted(out, key=lambda f: f.day)

    def download(self, key: str, dest: Path, attempts: int = 8) -> int:
        """Download to `dest` (atomically), backing off when the server throttles. Returns bytes written."""
        from boto3.s3.transfer import TransferConfig
        from botocore.exceptions import ClientError

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{os.getpid()}.{threading.get_ident()}.part")
        config = TransferConfig(multipart_chunksize=64 * 1024 * 1024, max_concurrency=8)
        delay = 2.0
        try:
            for attempt in range(attempts):
                try:
                    self._s3.download_file(self.bucket, key, str(tmp), Config=config)
                    break
                except ClientError as exc:
                    code = str(exc.response.get("Error", {}).get("Code", ""))
                    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                    throttled = status in (429, 500, 502, 503, 504) or code in THROTTLE_CODES
                    if not throttled or attempt == attempts - 1:
                        raise
                    wait = delay + random.uniform(0, delay)
                    log.debug("throttled on %s (%s); retrying in %.0fs", key, code or status, wait)
                    time.sleep(wait)
                    delay = min(delay * 2, 120)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        return dest.stat().st_size
