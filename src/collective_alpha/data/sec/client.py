"""SEC EDGAR HTTP client: declared user agent, 10 req/s fair-access limit, retries."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

log = logging.getLogger(__name__)

COMPANYFACTS_ZIP = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
COMPANYFACTS_API = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


class SecHTTPError(RuntimeError):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, SecHTTPError):
        return exc.status in (429, 500, 502, 503, 504)
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


class SecClient:
    def __init__(self, user_agent: str, max_rps: float = 8.0, timeout: float = 60.0):
        if "@" not in user_agent:
            raise ValueError("SEC requires a user agent with a contact e-mail (set CA_SEC_USER_AGENT)")
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=timeout,
            follow_redirects=True,
        )
        self._min_interval = 1.0 / max_rps
        self._lock = threading.Lock()
        self._last = 0.0

    def _throttle(self) -> None:
        with self._lock:
            wait = self._last + self._min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    @retry(
        retry=retry_if_exception(_retryable),
        wait=wait_exponential_jitter(initial=2, max=60),
        stop=stop_after_attempt(6),
        reraise=True,
    )
    def get_json(self, url: str) -> dict | None:
        self._throttle()
        r = self._client.get(url)
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise SecHTTPError(r.status_code, url)
        return r.json()

    def companyfacts(self, cik: str) -> dict | None:
        return self.get_json(COMPANYFACTS_API.format(cik=str(cik).zfill(10)))

    def head(self, url: str) -> httpx.Headers:
        self._throttle()
        r = self._client.head(url)
        if r.status_code >= 400:
            raise SecHTTPError(r.status_code, url)
        return r.headers

    def download(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        self._throttle()
        with self._client.stream("GET", url) as r:
            if r.status_code >= 400:
                raise SecHTTPError(r.status_code, url)
            with tmp.open("wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
        tmp.replace(dest)
        return dest

    def close(self) -> None:
        self._client.close()
