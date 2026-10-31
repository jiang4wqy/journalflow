from __future__ import annotations

import ipaddress
import json
import socket
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx

from .config import Config
from .state import State
from .util import public_url


class FetchError(RuntimeError):
    pass


class AccessRequired(FetchError):
    pass


class RetryLater(FetchError):
    def __init__(self, message: str, retry_at: str):
        super().__init__(message)
        self.retry_at = retry_at


def retry_time(value: str | None) -> str:
    current = datetime.now(timezone.utc)
    try:
        target = current + timedelta(seconds=max(1, int(value)))
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(value).astimezone(timezone.utc)
        except (TypeError, ValueError, AttributeError):
            target = current + timedelta(minutes=15)
    return max(target, current + timedelta(seconds=1)).isoformat()


class Http:
    def __init__(self, config: Config, *, state: State | None = None, transport=None, validate_hosts: bool = True):
        self.client = httpx.Client(timeout=config.request_timeout_seconds, transport=transport,
                                   headers={"User-Agent": "JournalFlow/0.1 (+https://github.com/jiang4wqy/journalflow)"})
        self.interval = config.request_interval_seconds
        self.validate_hosts = validate_hosts
        self.last_request = 0.0
        self.blocked = {}
        self.state = state

    def close(self):
        self.client.close()

    def validate_url(self, url: str):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise FetchError("Expected an HTTP(S) URL without embedded credentials")
        if self.validate_hosts:
            try:
                addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
            except OSError as error:
                raise FetchError(f"DNS resolution failed for {parts.hostname}") from error
            if any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
                raise FetchError("Private, loopback, and link-local download addresses are not allowed")

    @contextmanager
    def stream(self, url: str, params: dict | None = None):
        for _ in range(6):
            self.validate_url(url)
            origin = urlsplit(url).netloc.lower()
            saved = self.state.origin_block(origin) if self.state else None
            if saved:
                if saved["retry_at"]:
                    raise RetryLater(saved["last_error"], saved["retry_at"])
                raise AccessRequired(saved["last_error"])
            if origin in self.blocked:
                raise self.blocked[origin]
            time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                with self.client.stream("GET", url, params=params, follow_redirects=False) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError("Redirect response has no Location header")
                        url = urljoin(str(response.url), location)
                        params = None
                        continue
                    if response.status_code in (401, 403):
                        error = AccessRequired(f"Access requires user action: {public_url(str(response.url))}")
                        self.blocked[origin] = error
                        if self.state:
                            self.state.block_origin(origin, str(error))
                        raise error
                    if response.status_code == 429:
                        error = RetryLater(f"Rate limited: {public_url(str(response.url))}",
                                           retry_time(response.headers.get("retry-after")))
                        self.blocked[origin] = error
                        if self.state:
                            self.state.block_origin(origin, str(error), error.retry_at)
                        raise error
                    if response.status_code >= 500:
                        raise RetryLater(f"Source temporarily unavailable (HTTP {response.status_code})",
                                         retry_time(response.headers.get("retry-after")))
                    if response.status_code >= 400:
                        raise FetchError(f"HTTP {response.status_code}: {public_url(str(response.url))}")
                    if self.state:
                        self.state.clear_origin(origin)
                    yield response
                    return
            except httpx.HTTPError as error:
                raise FetchError(f"Network failure ({type(error).__name__}): {public_url(url)}") from error
        raise FetchError("Too many redirects")

    def json(self, url: str, params: dict | None = None):
        with self.stream(url, params) as response:
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 32 * 1024 * 1024:
                    raise FetchError("Metadata response exceeds 32 MiB")
            try:
                return json.loads(body)
            except (ValueError, UnicodeError) as error:
                raise FetchError(f"Expected JSON metadata: {public_url(str(response.url))}") from error
