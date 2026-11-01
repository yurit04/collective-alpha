"""SEC HTTP client: required User-Agent, a shared rate limit across threads, retries."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import orjson

log = logging.getLogger("edgar_cortical_node")

RETRY_STATUS = {429, 500, 502, 503, 504}
BULK_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{q}/master.{ymd}.idx"


class SecError(RuntimeError):
    def __init__(self, status: int, url: str, message: str = ""):
        super().__init__(f"HTTP {status} for {url}{': ' + message if message else ''}")
        self.status = status


class SecClient:
    def __init__(self, user_agent: str, requests_per_second: float = 8.0, timeout: float = 60.0,
                 max_retries: int = 6, transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self._http = httpx.Client(headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
                                  timeout=timeout, follow_redirects=True, transport=transport)
        self._interval = 1.0 / max(requests_per_second, 0.1)
        self._lock = threading.Lock()
        self._next = 0.0
        self.max_retries = max_retries
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self._interval
        if wait > 0:
            self._sleep(wait)

    def _request(self, method: str, url: str, stream: bool = False) -> httpx.Response:
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                if stream:
                    req = self._http.build_request(method, url)
                    resp = self._http.send(req, stream=True)
                else:
                    resp = self._http.request(method, url)
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise SecError(0, url, f"network error: {exc}") from exc
                self._sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                if stream:
                    resp.close()
                wait = float(resp.headers.get("Retry-After") or delay)
                log.warning("HTTP %s on %s; retrying in %.0fs", resp.status_code, url, wait)
                self._sleep(wait)
                delay = min(delay * 2, 60)
                continue
            return resp
        raise AssertionError("unreachable")

    def get_json(self, url: str) -> Any | None:
        """Decoded JSON, or None for 404 (e.g. a company without XBRL facts)."""
        resp = self._request("GET", url)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise SecError(resp.status_code, url, resp.text[:200])
        return orjson.loads(resp.content)

    def get_text(self, url: str) -> str | None:
        """Text, or None for 404/403 (daily index files do not exist on weekends and holidays)."""
        resp = self._request("GET", url)
        if resp.status_code in (403, 404):
            return None
        if resp.status_code != 200:
            raise SecError(resp.status_code, url, resp.text[:200])
        return resp.text

    def last_modified(self, url: str) -> str | None:
        resp = self._request("HEAD", url)
        if resp.status_code != 200:
            raise SecError(resp.status_code, url)
        return resp.headers.get("last-modified")

    def download(self, url: str, dest: Path, progress_every: int = 200 << 20) -> int:
        """Stream a large file to `dest` (written to a temp file, then renamed)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        resp = self._request("GET", url, stream=True)
        try:
            if resp.status_code != 200:
                raise SecError(resp.status_code, url)
            total = int(resp.headers.get("content-length") or 0)
            done, mark = 0, progress_every
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if done >= mark:
                        log.info("download: %.0f / %.0f MB", done / 1e6, total / 1e6)
                        mark += progress_every
            if total and done != total:
                raise SecError(200, url, f"incomplete download ({done} of {total} bytes)")
            os.replace(tmp, dest)
            return done
        finally:
            resp.close()
            tmp.unlink(missing_ok=True)
