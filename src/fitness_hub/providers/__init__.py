"""Provider plumbing shared by Strava, Garmin and Google Health."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Protocol

from ..http import RateLimited
from ..util import cursor_date

if TYPE_CHECKING:
    from ..db import Store

log = logging.getLogger(__name__)

# Suffix of a cursor recorded by a stream that stopped partway (e.g. Garmin
# throttling). The next run resumes on the following day, without the
# lookback overlap, so a throttled backfill always moves forward.
PARTIAL_SUFFIX = "/partial"


def partial_cursor(day: date) -> str:
    return day.isoformat() + PARTIAL_SUFFIX


@dataclass
class SyncResult:
    source: str
    stream: str
    rows: int = 0
    error: str | None = None
    cursor: str | None = None
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class SyncWindow:
    """Inclusive date window ``[start, end]`` on the athlete's local clock.

    ``explicit_start`` is true when the user asked for ``--since``: an
    incremental run then reaches back at least that far.
    """

    start: date
    end: date
    explicit_start: bool = False

    def start_for(self, cursor: str | None, lookback_days: int, full: bool) -> date:
        """First day a stream must fetch.

        ``full`` or no cursor: the window start. A partial cursor (see
        :data:`PARTIAL_SUFFIX`): the day after it. Otherwise the cursor minus
        the lookback overlap. Clamped to the window end, and extended back to
        an explicit ``--since``.
        """
        last = cursor_date(cursor)
        if full or last is None:
            return self.start
        if cursor and cursor.endswith(PARTIAL_SUFFIX):
            start = min(last + timedelta(days=1), self.end)
        else:
            start = min(last - timedelta(days=lookback_days), self.end)
        if self.explicit_start:
            start = min(start, self.start)
        return start


@dataclass
class StreamProgress:
    """Mutable progress a stream reports while it runs.

    ``cursor`` is persisted on success, and also on failure: a stream only
    sets it mid-run (as a :func:`partial_cursor`) when everything up to that
    day is safely stored.
    """

    rows: int = 0
    cursor: str | None = None
    notes: list[str] = field(default_factory=list)


class Provider(Protocol):
    name: str

    def configured(self) -> bool: ...

    def sync(self, store: Store, window: SyncWindow, full: bool = False) -> list[SyncResult]: ...


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, RateLimited):
        return str(exc)
    text = str(exc) or type(exc).__name__
    if type(exc).__name__ not in text:
        text = f"{type(exc).__name__}: {text}"
    return text[:1000]


def run_day_stream(
    store: Store,
    source: str,
    stream: str,
    window: SyncWindow,
    lookback_days: int,
    full: bool,
    body: Callable[[date, date, StreamProgress], None],
) -> SyncResult:
    """Run a stream that fetches a day range: start from the stored cursor (see
    :meth:`SyncWindow.start_for`) and move the cursor to the window end once
    ``body`` has stored everything."""

    def func(progress: StreamProgress) -> None:
        start = window.start_for(store.get_cursor(source, stream), lookback_days, full)
        body(start, window.end, progress)
        progress.cursor = window.end.isoformat()

    return run_stream(store, source, stream, func)


def run_stream(
    store: Store,
    source: str,
    stream: str,
    func: Callable[[StreamProgress], None],
) -> SyncResult:
    """Run one stream, record the outcome in ``sync_state`` and never raise
    for provider failures (one broken stream must not stop the others)."""
    progress = StreamProgress()
    try:
        func(progress)
    except Exception as exc:  # noqa: BLE001 - fail soft, but record it
        message = describe_error(exc)
        log.warning("%s/%s failed after %d rows: %s", source, stream, progress.rows, message)
        log.debug("%s/%s traceback", source, stream, exc_info=True)
        store.record_sync(source, stream, cursor=progress.cursor, error=message, rows=progress.rows)
        return SyncResult(source, stream, progress.rows, message, progress.cursor)
    store.record_sync(source, stream, cursor=progress.cursor, rows=progress.rows)
    return SyncResult(source, stream, progress.rows, None, progress.cursor)
