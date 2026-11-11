"""HTTP access to FINRA: the public Query API (short interest) and the CDN (daily files).

One shared rate limit across threads, retries with backoff on 429/5xx and network errors.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

API_URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
CDN_URL = "https://cdn.finra.org/equity/regsho/daily/{facility}shvol{day:%Y%m%d}.txt"
PAGE = 5000  # the API's maximum records per request


class FinraError(RuntimeError):
    pass


class FinraClient:
    def __init__(self, user_agent: str, requests_per_second: float = 5.0, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, retries: int = 5):
        self._http = httpx.Client(headers={"User-Agent": user_agent, "Accept": "application/json"},
                                  timeout=60, transport=transport, follow_redirects=True)
        self._interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0
        self._sleep = sleep
        self._retries = retries

    def close(self) -> None:
        self._http.close()

    def _wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self._interval
        if wait > 0:
            self._sleep(wait)

    def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        delay = 1.0
        for attempt in range(self._retries + 1):
            self._wait()
            try:
                resp = self._http.request(method, url, **kw)
            except httpx.TransportError as exc:
                if attempt == self._retries:
                    raise FinraError(f"{url}: {exc}") from exc
            else:
                if resp.status_code != 429 and resp.status_code < 500:
                    return resp
                if attempt == self._retries:
                    raise FinraError(f"{url}: HTTP {resp.status_code}")
            self._sleep(delay)
            delay = min(delay * 2, 60)
        raise AssertionError("unreachable")

    def short_interest_count(self, settlement_date: str) -> int:
        """Rows FINRA holds for one settlement date (0: not a settlement date, or not yet published)."""
        resp = self._request("POST", API_URL, json=_si_query(settlement_date, limit=1))
        _raise(resp, API_URL)
        return int(resp.headers.get("record-total", 0))

    def short_interest(self, settlement_date: str) -> list[dict[str, Any]]:
        """Every row for one settlement date (paged by PAGE). The Record-Total header says when
        to stop; without it, paging goes on until a page comes back short."""
        rows: list[dict[str, Any]] = []
        while True:
            resp = self._request("POST", API_URL, json=_si_query(settlement_date, limit=PAGE, offset=len(rows)))
            _raise(resp, API_URL)
            page = resp.json() if resp.content else []
            rows += page
            total = resp.headers.get("record-total")
            if not page or (len(rows) >= int(total) if total is not None else len(page) < PAGE):
                return rows

    def daily_file(self, facility: str, day) -> str | None:
        """A Reg SHO daily short sale volume file, or None when FINRA has none for that day
        (weekend, holiday, before the facility existed, or not posted yet)."""
        url = CDN_URL.format(facility=facility, day=day)
        resp = self._request("GET", url)
        if resp.status_code in (403, 404):
            return None
        _raise(resp, url)
        return resp.text


def _si_query(settlement_date: str, limit: int, offset: int = 0) -> dict[str, Any]:
    return {"limit": limit, "offset": offset,
            "compareFilters": [{"compareType": "EQUAL", "fieldName": "settlementDate", "fieldValue": settlement_date}]}


def _raise(resp: httpx.Response, url: str) -> None:
    if resp.status_code >= 400:
        raise FinraError(f"{url}: HTTP {resp.status_code}: {resp.text[:200]}")
