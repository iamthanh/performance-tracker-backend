"""``fitness-hub`` command line: auth, sync, status, import, MCP server, daemon."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from collections.abc import Sequence
from contextlib import closing

import httpx

from . import __version__
from .auth import AuthError, TokenStore
from .config import ConfigError, Settings
from .db import open_readonly, row_counts, sync_states
from .http import ApiError
from .sync import PROVIDER_NAMES, SyncInProgress, build_providers, run_sync

log = logging.getLogger("fitness_hub")


def _setup_logging(verbose: bool, command: str | None) -> None:
    level = logging.DEBUG if verbose else (logging.INFO if command == "run" else logging.WARNING)
    # Always stderr: with the stdio MCP transport stdout carries the protocol.
    logging.basicConfig(
        level=level, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True
    )
    logging.getLogger("httpx").setLevel(logging.DEBUG if verbose else logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fitness-hub",
        description="Local-first hub for Strava, Garmin Connect and Google Health data, served to Claude over MCP.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging (to stderr)")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    auth = sub.add_parser("auth", help="authorize a service (one-time, interactive)")
    auth.add_argument("service", choices=("strava", "google", "garmin"))
    auth.add_argument("--no-browser", action="store_true", help="print the consent URL without opening a browser")

    sync = sub.add_parser("sync", help="fetch new data from authorized services")
    sync.add_argument(
        "--source", action="append", choices=PROVIDER_NAMES, help="limit to this source (repeatable)"
    )
    sync.add_argument("--since", metavar="YYYY-MM-DD", help="fetch at least back to this date")
    sync.add_argument("--full", action="store_true", help="ignore cursors and re-fetch the whole window")

    sub.add_parser("status", help="show authorization, sync state and row counts")

    imp = sub.add_parser("import-garmin-csv", help="import a Garmin Connect 'Export CSV' activity list")
    imp.add_argument("path")
    imp.add_argument("--distance-unit", choices=("km", "mi"), default="km", help="unit of the Distance column")
    imp.add_argument(
        "--timezone",
        metavar="TZ",
        help="IANA zone of the Date column (e.g. Europe/Berlin); improves de-duplication",
    )

    serve = sub.add_parser("serve-mcp", help="run the MCP server (stdio for Claude, or localhost HTTP)")
    serve.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    serve.add_argument("--host", default="127.0.0.1", help="HTTP bind address (loopback only)")
    serve.add_argument("--port", type=int, default=8800)

    run = sub.add_parser("run", help="daemon: sync every N minutes (optionally also serve MCP over HTTP)")
    run.add_argument("--interval-minutes", type=float, default=60.0)
    run.add_argument("--http", action="store_true", help="also serve MCP over HTTP on --host/--port")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8800)
    return parser


# -- commands -------------------------------------------------------------------------


def cmd_auth(settings: Settings, args: argparse.Namespace) -> int:
    settings.ensure_home()
    tokens = TokenStore(settings.tokens_path)
    if args.service == "strava":
        from .providers.strava import authorize

        info = authorize(settings, tokens, open_browser=not args.no_browser)
        print(f"Strava authorized (athlete {info.get('athlete_id')}, scope {info.get('scope')}).")
    elif args.service == "google":
        from .providers.google_health import authorize as google_authorize

        info = google_authorize(settings, tokens, open_browser=not args.no_browser)
        print(f"Google Health authorized (health user {info.get('health_user_id')}).")
        print(
            "Note: while the OAuth consent screen is in 'Testing' status Google expires the refresh "
            "token after 7 days; re-run `fitness-hub auth google` when sync reports invalid_grant."
        )
    else:
        from .providers.garmin import authorize as garmin_authorize

        info = garmin_authorize(settings)
        print(f"Garmin Connect authorized{' for ' + info['name'] if info.get('name') else ''}.")
    print(f"Next: fitness-hub sync --source {'google_health' if args.service == 'google' else args.service}")
    return 0


def cmd_sync(settings: Settings, args: argparse.Namespace) -> int:
    results = run_sync(settings, sources=args.source, since=args.since, full=args.full, log_line=print)
    return 1 if any(r.error for r in results) else 0


def cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    print(f"Home:     {settings.home}")
    print(f"Database: {settings.db_path}")
    providers = build_providers(settings)
    for name in PROVIDER_NAMES:
        try:
            state = "authorized" if providers[name].configured() else "not authorized"
        except Exception as exc:  # noqa: BLE001 - e.g. a corrupt tokens.json; show it, keep going
            state = f"error: {exc}"
        print(f"  {name:<14} {state}")
    with closing(open_readonly(settings.db_path)) as conn:
        counts = row_counts(conn)
        states = sync_states(conn)
    print("Rows:     " + ", ".join(f"{table}={n}" for table, n in counts.items()))
    if not states:
        print("No syncs recorded yet. Run `fitness-hub sync`.")
        return 0
    print()
    header = f"{'SOURCE':<14} {'STREAM':<30} {'LAST SUCCESS':<21} {'CURSOR':<21} {'ROWS':>6}"
    print(header)
    print("-" * len(header))
    for s in states:
        print(
            f"{s['source']:<14} {s['stream']:<30} {s['last_success'] or '-':<21} "
            f"{s['cursor'] or '-':<21} {s['rows_upserted'] if s['rows_upserted'] is not None else '-':>6}"
        )
        if s["last_error"]:
            print(f"{'':<14} ! last error ({s['last_attempt']}): {s['last_error']}")
    return 0


def cmd_import(settings: Settings, args: argparse.Namespace) -> int:
    from .db import Store
    from .providers.garmin import import_garmin_csv

    with Store(settings.db_path) as store:
        result = import_garmin_csv(store, args.path, distance_unit=args.distance_unit, timezone=args.timezone)
    print(f"Imported {result['imported']} activities ({result['skipped']} rows skipped) as source garmin_export.")
    if not args.timezone:
        print("Tip: pass --timezone so these activities de-duplicate against other sources.")
    return 0


def cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    from .mcp_server import serve

    serve(settings, transport=args.transport, host=args.host, port=args.port)
    return 0


def _sync_forever(settings: Settings, interval_s: float, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            results = run_sync(settings)
            errors = [r for r in results if r.error]
            if errors:
                log.warning("sync finished with %d stream error(s); see `fitness-hub status`", len(errors))
        except SyncInProgress as exc:
            log.info("%s", exc)
        except Exception:  # noqa: BLE001 - the daemon must survive anything
            log.exception("sync failed; retrying in %.0f minutes", interval_s / 60)
        stop.wait(interval_s)


def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    if args.interval_minutes <= 0:
        raise ValueError("--interval-minutes must be positive")
    interval_s = args.interval_minutes * 60
    stop = threading.Event()
    log.info("fitness-hub daemon: syncing every %.0f minutes", args.interval_minutes)
    if not args.http:
        try:
            _sync_forever(settings, interval_s, stop)
        except KeyboardInterrupt:
            pass
        return 0
    from .mcp_server import check_http_host, serve

    check_http_host(args.host)  # fail before starting the sync thread
    worker = threading.Thread(target=_sync_forever, args=(settings, interval_s, stop), daemon=True, name="sync")
    worker.start()
    try:
        serve(settings, transport="http", host=args.host, port=args.port)
    finally:
        stop.set()
    return 0


COMMANDS = {
    "auth": cmd_auth,
    "sync": cmd_sync,
    "status": cmd_status,
    "import-garmin-csv": cmd_import,
    "serve-mcp": cmd_serve,
    "run": cmd_run,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    _setup_logging(args.verbose, args.command)
    try:
        settings = Settings.from_env()
        return COMMANDS[args.command](settings, args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except (AuthError, SyncInProgress, ValueError, ApiError, httpx.HTTPError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
