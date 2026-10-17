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


_THRESHOLD_METRICS = {"ftp": "ftp_w", "threshold-hr": "threshold_hr", "max-hr": "max_hr", "resting-hr": "resting_hr"}


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

    strava = sub.add_parser("import-strava-export", help="import an unzipped Strava bulk export (activities + files)")
    strava.add_argument("path", help="the unzipped export folder (it contains activities.csv)")
    strava.add_argument(
        "--timezone",
        metavar="TZ",
        help="IANA zone for activities whose file has no usable local time (GPX, Zwift), e.g. America/Los_Angeles",
    )
    strava.add_argument(
        "--distance-unit", choices=("km", "mi"), default="km", help="your Strava account's distance unit"
    )
    strava.add_argument("--force", action="store_true", help="re-parse files whose streams are already stored")
    strava.add_argument("--workers", type=int, default=None, help="parallel file parsers (default: up to 8)")

    analyze = sub.add_parser("analyze", help="compute TSS, NP, zones, power curves and auto FTP from stored streams")
    analyze.add_argument("--full", action="store_true", help="re-analyse every activity, not only new ones")

    ftp = sub.add_parser("thresholds", help="show or override FTP, threshold HR, max HR and resting HR over time")
    ftp_sub = ftp.add_subparsers(dest="action", metavar="ACTION")
    ftp_sub.add_parser("list", help="every dated value, manual and auto")
    ftp_set = ftp_sub.add_parser("set", help="set a manual value from a date on (re-scores affected activities)")
    ftp_set.add_argument("metric", choices=tuple(_THRESHOLD_METRICS))
    ftp_set.add_argument("value", type=float)
    ftp_set.add_argument("--from", dest="effective_date", required=True, metavar="YYYY-MM-DD")
    ftp_set.add_argument("--note")
    ftp_del = ftp_sub.add_parser("delete", help="remove a manual value")
    ftp_del.add_argument("metric", choices=tuple(_THRESHOLD_METRICS))
    ftp_del.add_argument("--from", dest="effective_date", required=True, metavar="YYYY-MM-DD")

    perf = sub.add_parser("performance", help="show the performance index (or check its decay against your rides)")
    perf.add_argument(
        "--backtest", action="store_true", help="compare decay rates by how well they predict your efforts"
    )

    serve = sub.add_parser("serve-mcp", help="run the MCP server (stdio for Claude, or localhost HTTP)")
    serve.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    serve.add_argument("--host", default="127.0.0.1", help="HTTP bind address (loopback only)")
    serve.add_argument("--port", type=int, default=8800)

    api = sub.add_parser("serve-api", help="serve the read API for the web UI")
    api.add_argument(
        "--host",
        default=None,
        help="bind address (default API_HOST or 127.0.0.1); 0.0.0.0 serves the local network, without authentication",
    )
    api.add_argument("--port", type=int, default=8801)

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


def cmd_import_strava(settings: Settings, args: argparse.Namespace) -> int:
    from .db import Store
    from .providers.strava_export import import_strava_export

    def progress(done: int, total: int) -> None:
        if done == total or done % 100 == 0:
            print(f"  parsed {done}/{total} activity files", file=sys.stderr)

    with Store(settings.db_path) as store:
        result = import_strava_export(
            store,
            args.path,
            timezone=args.timezone,
            distance_unit=args.distance_unit,
            force=args.force,
            workers=args.workers,
            progress=progress,
        )
    print(
        f"Imported {result['activities']} Strava activities; streams parsed for {result['streams_imported']} "
        f"({result['streams_already_stored']} already stored)."
    )
    if result["api_rows_kept"]:
        print(f"Kept {result['api_rows_kept']} activities synced from the Strava API as they were (streams added).")
    problems = {k: v for k, v in result.items() if k.startswith(("files_", "rows_")) and v}
    if problems:
        print("Not imported: " + ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in problems.items()) + " (see -v)")
    if result["rows_skipped"] and not (result["activities"] or result["api_rows_kept"]):
        print("error: no rows of activities.csv could be read (unexpected date format?)", file=sys.stderr)
        return 1
    return _analyze(settings, full=False)


def _analyze(settings: Settings, full: bool) -> int:
    from .analysis import analyze
    from .db import Store

    def progress(done: int, total: int) -> None:
        if done == total or done % 200 == 0:
            print(f"  analysed {done}/{total} activities", file=sys.stderr)

    with Store(settings.db_path) as store:
        result = analyze(store, full=full, progress=progress)
    print(
        f"Analysis: {result['analysed']} activities analysed, {result['rescored']} re-scored "
        f"({result['ftp_changes']} FTP and {result['threshold_hr_changes']} threshold HR values in effect over time)."
    )
    return 0


def cmd_analyze(settings: Settings, args: argparse.Namespace) -> int:
    return _analyze(settings, full=args.full)



def cmd_thresholds(settings: Settings, args: argparse.Namespace) -> int:
    from .db import Store

    if args.action in ("set", "delete"):
        metric = _THRESHOLD_METRICS[args.metric]
        with Store(settings.db_path) as store:
            if args.action == "set":
                store.set_athlete_setting(metric, args.effective_date, args.value, note=args.note)
            elif not store.delete_athlete_setting(metric, args.effective_date):
                print(f"error: no manual {args.metric} value from {args.effective_date}", file=sys.stderr)
                return 1
        return _analyze(settings, full=False)
    with closing(open_readonly(settings.db_path)) as conn:
        rows = conn.execute(
            "SELECT metric, effective_date, origin, value, note FROM athlete_settings ORDER BY metric, effective_date"
        ).fetchall()
    if not rows:
        print("No thresholds yet. Import activities, or run `fitness-hub analyze`.")
        return 0
    print(f"{'METRIC':<14} {'FROM':<11} {'ORIGIN':<7} {'VALUE':>6}  NOTE")
    for r in rows:
        print(f"{r['metric']:<14} {r['effective_date']:<11} {r['origin']:<7} {r['value']:>6g}  {r['note'] or ''}")
    return 0


def cmd_performance(settings: Settings, args: argparse.Namespace) -> int:
    from . import performance

    with closing(open_readonly(settings.db_path)) as conn:
        if args.backtest:
            print(f"{'DECAY/WEEK':>10} {'SAMPLES':>8} {'MAE %':>7} {'BIAS %':>7} {'UNDER %':>8}")
            for r in performance.backtest(conn):
                mae, bias, under = r["mae_pct"] or 0, r["bias_pct"] or 0, r["under_pct"] or 0
                print(f"{r['decay_per_week']:>10.3f} {r['samples']:>8} {mae:>7.2f} {bias:>7.2f} {under:>8.1f}")
            print("Against the best effort of the next 28 days, on the days that held a hard one. MAE: mean error;")
            print("BIAS: below 0, the value runs above what you then did; UNDER: efforts over 3 % above the value.")
            return 0
        result = performance.performance(conn)
    if not result["days"]:
        print("No rides with power yet. Import activities, then run `fitness-hub analyze`.")
        return 0
    today = result["days"][-1]
    print(f"Performance index on {today['date']}: {today['score'] if today['score'] is not None else '—'}")
    for c in result["components"]:
        part = today["components"][c["key"]]
        index = "—" if part["index"] is None else f"{part['index']:.1f}"
        print(f"  {c['label']:<11} {index:>6}  (last tested {part['tested'] or 'never'})")
    return 0


def cmd_serve_api(settings: Settings, args: argparse.Namespace) -> int:
    from .api import serve

    serve(settings, host=args.host, port=args.port)
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
    "import-strava-export": cmd_import_strava,
    "analyze": cmd_analyze,
    "thresholds": cmd_thresholds,
    "performance": cmd_performance,
    "serve-mcp": cmd_serve,
    "serve-api": cmd_serve_api,
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
