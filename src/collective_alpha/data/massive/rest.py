"""Thin, dependency-light REST client for Massive (Polygon-compatible API).

We keep our own client instead of the vendor SDK's typed models so that raw JSON pages
can be archived verbatim under raw/rest and re-parsed later if schemas evolve.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

log = logging.getLogger(__name__)


class MassiveHTTPError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status} for {url}: {body[:300]}")
        self.status = status
        self.url = url


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, MassiveHTTPError):
        return exc.status in (408, 425, 429, 500, 502, 503, 504)
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


class RestClient:
    def __init__(self, api_key: str, base_url: str = "https://api.massive.com", timeout: float = 60.0):
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "collective-alpha/0.1"},
            timeout=timeout,
            http2=False,
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    @retry(
        retry=retry_if_exception(_retryable),
        wait=wait_exponential_jitter(initial=1, max=60),
        stop=stop_after_attempt(8),
        reraise=True,
    )
    def get_json(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        r = self._client.get(path_or_url, params=params)
        if r.status_code == 429:
            retry_after = float(r.headers.get("Retry-After", "5"))
            log.warning("rate limited, sleeping %.1fs", retry_after)
            time.sleep(retry_after)
        if r.status_code >= 400:
            raise MassiveHTTPError(r.status_code, str(r.request.url).split("?")[0], r.text)
        data = r.json()
        if isinstance(data, list):  # e.g. /v1/marketstatus/upcoming returns a bare list
            return {"results": data, "status": "OK"}
        return data

    def paginate(
        self, path: str, params: dict[str, Any] | None = None, max_pages: int | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield each raw page. Follows `next_url` (which lacks the auth header -> we add it)."""
        page = self.get_json(path, params)
        n = 1
        yield page
        while page.get("next_url") and (max_pages is None or n < max_pages):
            page = self.get_json(page["next_url"])
            n += 1
            yield page

    def results(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in self.paginate(path, params):
            res = page.get("results")
            if isinstance(res, list):
                out.extend(res)
            elif isinstance(res, dict):
                out.append(res)
        return out

    def fetch_all_pages(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return list(self.paginate(path, params))


def save_pages(pages: list[dict[str, Any]], path: Path) -> int:
    """Archive raw pages (gzip JSON) and return the number of result rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(pages, fh)
    tmp.replace(path)
    n = 0
    for p in pages:
        res = p.get("results")
        n += len(res) if isinstance(res, list) else (1 if res else 0)
    return n


def load_pages(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def flatten_results(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in pages:
        res = p.get("results")
        if isinstance(res, list):
            out.extend(res)
        elif isinstance(res, dict):
            out.append(res)
    return out
