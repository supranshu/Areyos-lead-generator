"""Shared HTTP client: honest user agent, per-host throttling, retry on transient errors."""

import threading
import time
from urllib.parse import urlsplit

import httpx

from areyos.config import get_settings


class TransientError(Exception):
    """Worth retrying later (rate limit, 5xx, network)."""


class PoliteClient:
    def __init__(self, per_host_delay: float | None = None, timeout: float | None = None):
        settings = get_settings()
        self.per_host_delay = settings.per_host_delay if per_host_delay is None else per_host_delay
        self._client = httpx.Client(
            timeout=timeout or settings.http_timeout,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        with self._lock:
            wait = self._last_hit.get(host, 0) + self.per_host_delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_hit[host] = time.monotonic()

    def request(self, method: str, url: str, retries: int = 2, **kwargs) -> httpx.Response:
        for attempt in range(retries + 1):
            self._throttle(url)
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt == retries:
                    raise TransientError(f"{method} {url}: {exc}") from exc
            else:
                if response.status_code != 429 and response.status_code < 500:
                    return response
                if attempt == retries:
                    raise TransientError(f"{method} {url}: HTTP {response.status_code}")
            time.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        self._client.close()
