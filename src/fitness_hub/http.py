"""JSON-over-HTTP helper with bounded retry and backoff."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Collection
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
# A non-idempotent request (e.g. the POST that rotates a Strava refresh token)
# is only retried when the server certainly did not process it.
NOT_PROCESSED_STATUSES = frozenset({429, 503})
_UNSENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class ApiError(Exception):
    """Non-success HTTP response."""

    def __init__(self, status: int, body: str, url: str):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status} from {url}: {body[:500]}")


class RateLimited(ApiError):
    """HTTP 429 that cannot be waited out within the retry budget."""

    def __init__(self, body: str, url: str, retry_after: float | None = None):
        super().__init__(429, body, url)
        self.retry_after = retry_after

    def __str__(self) -> str:
        hint = f" (retry after {self.retry_after:.0f}s)" if self.retry_after else ""
        return f"rate limited by {self.url}{hint}"


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:  # HTTP-date form is not honored; fall back to backoff
        return None


def _redacted_url(url: httpx.URL) -> str:
    # Query strings can carry filters but never tokens here; still drop them
    # from error messages to keep logs short and free of identifiers.
    return str(url.copy_with(query=None))


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retry_statuses: Collection[int] = RETRY_STATUSES,
    max_retries: int = 4,
    base_delay: float = 2.0,
    max_wait: float = 120.0,
    sleep: Callable[[float], None] = time.sleep,
    idempotent: bool | None = None,
    **kwargs: Any,
) -> Any:
    """Send a request and decode the JSON body.

    Retries ``retry_statuses`` and transport errors with exponential backoff
    (``base_delay`` doubling, at most ``max_retries`` retries), honoring a
    numeric ``Retry-After``. A 429 whose required wait exceeds ``max_wait``
    (or a 429 not in ``retry_statuses``) raises :class:`RateLimited` at once
    instead of blocking the sync. Other statuses >= 400 raise :class:`ApiError`.
    Returns ``None`` for an empty body.

    ``idempotent`` defaults from the method. Non-idempotent requests are only
    retried when they were certainly not processed (connection never made,
    429 or 503), so a refresh-token POST that reached the server is never
    replayed with a token it has already rotated.
    """
    if idempotent is None:
        idempotent = method.upper() in IDEMPOTENT_METHODS
    if not idempotent:
        retry_statuses = frozenset(retry_statuses) & NOT_PROCESSED_STATUSES
    attempt = 0
    while True:
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if attempt >= max_retries or not (idempotent or isinstance(exc, _UNSENT_ERRORS)):
                raise
            delay = base_delay * (2**attempt)
            log.warning("%s %s failed (%s); retrying in %.0fs", method, url, exc, delay)
            sleep(delay)
            attempt += 1
            continue

        status = response.status_code
        if status in retry_statuses or status == 429:
            wait = _retry_after(response)
            delay = wait if wait is not None else base_delay * (2**attempt)
            target = _redacted_url(response.request.url)
            if status == 429 and (
                status not in retry_statuses or delay > max_wait or attempt >= max_retries
            ):
                raise RateLimited(response.text, target, retry_after=wait)
            if attempt >= max_retries or delay > max_wait:
                raise ApiError(status, response.text, target)
            log.warning("HTTP %s from %s; retrying in %.0fs", status, target, delay)
            sleep(delay)
            attempt += 1
            continue

        if status >= 400:
            raise ApiError(status, response.text, _redacted_url(response.request.url))
        if not response.content or not response.content.strip():
            return None
        try:
            return response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(
                status, "invalid JSON: " + response.text[:200], _redacted_url(response.request.url)
            ) from None


def new_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=DEFAULT_TIMEOUT,
        transport=transport,
        headers={"User-Agent": "fitness-hub/0.1 (+local)", "Accept": "application/json"},
        follow_redirects=False,
    )
