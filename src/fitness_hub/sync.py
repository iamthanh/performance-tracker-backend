"""Sync orchestration: build the window, run each authorized provider, log."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import date, timedelta

from .auth import TokenStore
from .config import Settings
from .db import Store
from .providers import Provider, SyncResult, SyncWindow, describe_error
from .providers.garmin import GarminProvider
from .providers.google_health import GoogleHealthProvider
from .providers.strava import StravaProvider
from .util import parse_date

log = logging.getLogger(__name__)

PROVIDER_NAMES = ("strava", "garmin", "google_health")
AUTH_COMMAND = {"strava": "strava", "garmin": "garmin", "google_health": "google"}


class SyncInProgress(RuntimeError):
    """Another sync holds the lock (e.g. the daemon while sync_now runs)."""


def build_providers(settings: Settings, tokens: TokenStore | None = None) -> dict[str, Provider]:
    tokens = tokens or TokenStore(settings.tokens_path)
    return {
        "strava": StravaProvider(settings, tokens),
        "garmin": GarminProvider(settings),
        "google_health": GoogleHealthProvider(settings, tokens),
    }


def make_window(settings: Settings, since: str | date | None = None, today: date | None = None) -> SyncWindow:
    end = today or date.today()
    if since is None:
        return SyncWindow(end - timedelta(days=settings.backfill_days), end)
    start = parse_date(since, "since")
    if start > end:
        raise ValueError(f"--since {start} is in the future")
    return SyncWindow(start, end, explicit_start=True)


@contextlib.contextmanager
def sync_lock(settings: Settings) -> Iterator[None]:
    """Process-wide exclusive lock so two syncs never race on token refresh."""
    try:
        import fcntl
    except ImportError:  # Windows: SQLite locking still keeps the data consistent
        yield
        return
    settings.ensure_home()
    with open(settings.home / "sync.lock", "a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncInProgress("another sync is already running; try again when it finishes") from None
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def run_sync(
    settings: Settings,
    sources: Sequence[str] | None = None,
    since: str | date | None = None,
    full: bool = False,
    *,
    providers: Mapping[str, Provider] | None = None,
    log_line: Callable[[str], None] | None = None,
    today: date | None = None,
) -> list[SyncResult]:
    """Sync the requested sources (default: all). Never raises for provider
    failures; they come back as results with ``error`` set and are recorded in
    ``sync_state``. Unauthorized sources are skipped."""
    names = list(dict.fromkeys(sources or PROVIDER_NAMES))
    unknown = [n for n in names if n not in PROVIDER_NAMES]
    if unknown:
        raise ValueError(f"unknown source(s): {', '.join(unknown)} (choose from {', '.join(PROVIDER_NAMES)})")
    window = make_window(settings, since, today)

    def emit(line: str) -> None:
        log.info(line)
        if log_line:
            log_line(line)

    owned = providers is None
    active = providers if providers is not None else build_providers(settings)
    results: list[SyncResult] = []
    try:
        with sync_lock(settings), Store(settings.db_path) as store:
            for name in names:
                provider = active[name]
                try:
                    if not provider.configured():
                        emit(f"{name}: skipped (not authorized; run `fitness-hub auth {AUTH_COMMAND[name]}`)")
                        results.append(SyncResult(name, "*", skipped=True))
                        continue
                    emit(f"{name}: syncing {window.start} .. {window.end}{' (full)' if full else ''}")
                    for result in provider.sync(store, window, full):
                        results.append(result)
                        status = f"ERROR: {result.error}" if result.error else "ok"
                        emit(f"  {name}/{result.stream}: {result.rows} rows, {status}")
                except Exception as exc:  # noqa: BLE001 - a provider bug must not stop the others
                    message = describe_error(exc)
                    log.debug("provider %s crashed", name, exc_info=True)
                    store.record_sync(name, "sync", error=message)
                    results.append(SyncResult(name, "sync", error=message))
                    emit(f"  {name}: ERROR: {message}")
    finally:
        if owned:
            for provider in active.values():
                close = getattr(provider, "close", None)
                if close:
                    close()
    errors = sum(1 for r in results if r.error)
    emit(f"done: {sum(r.rows for r in results)} rows upserted, {errors} stream error(s)")
    return results
