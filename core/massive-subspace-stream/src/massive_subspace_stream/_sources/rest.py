"""Small Massive REST client: bearer auth, retries with backoff, cursor pagination."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class RestError(RuntimeError):
    def __init__(self, status: int, path: str, message: str):
        super().__init__(f"HTTP {status} for {path}: {message}")
        self.status = status
        self.path = path


class NotEntitledError(RestError):
    """403: the subscription does not include this dataset."""


class RestClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.massive.com",
        timeout: float = 60.0,
        max_retries: int = 6,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ):
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "collective-alpha/massive-subspace-stream"},
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
        )
        self.max_retries = max_retries
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> RestClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a path (or a full `next_url`) and return the decoded JSON."""
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.get(path, params=params)
            except (httpx.TransportError, httpx.DecodingError) as exc:
                if attempt == self.max_retries:
                    raise RestError(0, _short(path), f"network error: {exc}") from exc
                log.warning("network error on %s (%s); retrying in %.0fs", _short(path), type(exc).__name__, delay)
                self._sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:  # a cut-off body or an error page sent with status 200
                    if attempt == self.max_retries:
                        raise RestError(200, _short(path), f"the response is not JSON ({exc})") from exc
                    log.warning("unreadable answer on %s; retrying in %.0fs", _short(path), delay)
                    self._sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                wait = float(resp.headers.get("Retry-After") or delay)
                log.warning("HTTP %s on %s; retrying in %.0fs", resp.status_code, _short(path), wait)
                self._sleep(wait)
                delay = min(delay * 2, 60)
                continue
            message = _error_message(resp)
            if resp.status_code == 403:
                raise NotEntitledError(403, _short(path), message)
            raise RestError(resp.status_code, _short(path), message)
        raise AssertionError("unreachable")

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> Iterator[list[dict[str, Any]]]:
        """Yield one page of `results` at a time, following `next_url` cursors."""
        page = self.get(path, params)
        while True:
            if isinstance(page, list):  # a few endpoints return a bare list
                yield page
                return
            results = page.get("results")
            if isinstance(results, dict):
                results = [results]
            yield results or []
            next_url = page.get("next_url")
            if not next_url:
                return
            page = self.get(next_url)


def _short(path: str) -> str:
    return path.split("?", 1)[0].replace("https://api.massive.com", "")


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return str(body.get("message") or body.get("error") or body)[:300]
    except ValueError:
        return resp.text[:300]
