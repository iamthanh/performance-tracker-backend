# fitness-hub-core

The core library of **fitness-hub**, a local-first hub that pulls your workouts and health metrics from
**Strava**, **Garmin Connect** and the **Google Health API** (the successor to the Fitbit Web API) into a
SQLite database on your machine, so Claude can read them over MCP.

fitness-hub is split across two repositories (see [PLAN.md §14](PLAN.md#14-if-the-owner-uses-multiple-repos)):

| Repository | Distribution | Contents |
|---|---|---|
| **performance-tracker-backend** (this repo) | `fitness-hub-core` | config, storage, HTTP/OAuth helpers, providers, sync, read-side queries |
| [performance-tracker](https://github.com/iamthanh/performance-tracker) | `fitness-hub-mcp` | the `fitness-hub` CLI and the MCP server |

**To install and use fitness-hub, follow the [performance-tracker README](https://github.com/iamthanh/performance-tracker#readme).**
This README describes the library: how data is stored, how syncing works and what each provider does.

Both distributions share the `fitness_hub` import package. This repo owns `fitness_hub/__init__.py`, which uses
`pkgutil.extend_path` so the app's `fitness_hub.cli` and `fitness_hub.mcp_server` load even when both are
editable installs from separate checkouts. They are versioned together (`0.1.x`).

## Development

```bash
python3.12 -m venv .venv                 # 3.11 works too, without the Garmin extra
.venv/bin/pip install -e '.[dev,garmin]'
.venv/bin/pytest                         # no network access needed: all HTTP is mocked
```

`garminconnect` (the `garmin` extra) requires Python 3.12 or newer. On 3.11 the extra installs nothing, the Garmin
provider reports how to install it, and the CSV importer still works.

## Layout

```
src/fitness_hub/
├── __init__.py          # __version__, shared-package path
├── config.py            # Settings (env vars + .env files)
├── db.py                # schema, Store (idempotent upserts, sync_state), open_readonly()
├── http.py              # request_json: retry/backoff, RateLimited, ApiError
├── auth.py              # TokenStore (0600, atomic), PKCE, local OAuth redirect server
├── util.py              # timestamp/number parsing helpers
├── sync.py              # build_providers, run_sync, sync lock
├── queries.py           # read side: activities, summaries, metrics, sleep, body, run_select, de-dup
└── providers/
    ├── __init__.py      # SyncWindow, SyncResult, run_stream
    ├── strava.py
    ├── google_health.py
    └── garmin.py        # provider + Garmin CSV importer
```

## Configuration

Settings come from environment variables, then `$FITNESS_HUB_HOME/.env`, then `./.env` (the real environment wins).
See [`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `FITNESS_HUB_HOME` | `~/.fitness-hub` | Holds `fitness.db`, `tokens.json`, `garmin-tokens/`, `.env` |
| `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` | – | Strava API application |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | – | Google OAuth client of type "Web application" |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | – | Optional; only skip the prompts of `fitness-hub auth garmin` |
| `OAUTH_PORT` | `8765` | Local OAuth redirect port (`http://localhost:<port>/callback`) |
| `BACKFILL_DAYS` | `365` | History fetched the first time a stream syncs |
| `LOOKBACK_DAYS` | `7` | Overlap re-fetched on every incremental sync |

Files in `FITNESS_HUB_HOME`:

- `fitness.db` (with `-wal`/`-shm`): created owner-only (0600) because it holds health data.
- `tokens.json`: OAuth tokens, mode 0600, written atomically (temp file + `os.replace`). Tokens are never stored in the
  database and never logged.
- `garmin-tokens/garmin_tokens.json`: Garmin session tokens (0600 in a 0700 directory). Your Garmin password is never
  written anywhere.
- `sync.lock`: stops two syncs (for example the daemon and an MCP `sync_now`) from running at once.

## Data model

SQLite in WAL mode. Timestamps are UTC ISO-8601 (`YYYY-MM-DDTHH:MM:SSZ`); dates are `YYYY-MM-DD` on the athlete's
local clock. Every row keeps the provider payload in `raw` and an `updated_at` stamp, and every write is an
`INSERT … ON CONFLICT DO UPDATE`, so re-syncing never duplicates rows.

| Table | Key | Columns |
|---|---|---|
| `activities` | `(source, source_id)` | `start_time, local_date, activity_type, name, duration_s, moving_s, distance_m, elevation_gain_m, avg_hr, max_hr, avg_power_w, calories, raw` |
| `daily_metrics` | `(source, date, metric)` | `value, raw` |
| `sleep_sessions` | `(source, source_id)` | `start_time, end_time, local_date` (the date the sleep **ended**), `minutes_asleep, minutes_awake, minutes_deep, minutes_light, minutes_rem, is_main, raw` |
| `body_measurements` | `(source, ts, metric)` | `value, raw` |
| `sync_state` | `(source, stream)` | `cursor, last_attempt, last_success, last_error, rows_upserted` |

**Sources:** `strava`, `garmin`, `google_health`, `garmin_export` (CSV import).

**Daily metrics** (stable names, part of the MCP contract): `steps, distance_m, total_calories_kcal,
active_calories_kcal, floors, active_zone_minutes, resting_hr, hrv_rmssd_ms, sleep_score, avg_stress,
body_battery_high, body_battery_low, spo2_pct, respiratory_rate_bpm, vo2_max`.
**Body metrics:** `weight_kg, body_fat_pct`.

## How syncing works

`run_sync(settings, sources=None, since=None, full=False)`:

1. The window runs from `today − BACKFILL_DAYS` (or `--since`) to today.
2. Sources that are not authorized are skipped, with the `fitness-hub auth …` command to run.
3. Each provider syncs its streams. Every stream runs inside `run_stream`, so a failure is recorded in `sync_state`
   and the other streams still run. A provider that crashes outright is recorded as a `sync` error.
4. The CLI exits with code 1 if any stream reported an error.

**Where a stream starts:** at the window start if `--full` is set or the stream has no cursor yet. Otherwise it
starts at `cursor − LOOKBACK_DAYS`, clamped to today. `--since` also pulls an incremental run back to that date.

**Cursors:** Strava `activities` stores the newest `start_time` seen. Day-based streams store the window end. Garmin
`daily` stores the last day that was fully synced, so a throttled run resumes where it stopped.

**Consistency:** each batch of rows is written in its own transaction, and a cursor only moves when everything before
it is stored. If a sync is killed partway, the database stays consistent and the next run fetches the gap again
(the upserts make that harmless). On an error, the stored cursor and `last_success` are kept, and a `NULL` cursor
never erases one that is already stored.

**HTTP:** `request_json` retries 429/502/503/504 and transport errors with exponential backoff (2 s, doubling, at most
4 retries) and honours a numeric `Retry-After`. A 429 that would need more than 120 s raises `RateLimited` straight
away. Strava's 429 is never retried, because its window is 15 minutes; the stream stops and records the error.

## Providers

### Strava (official API v3)

- OAuth authorization code with scopes `read,activity:read_all,profile:read_all` and redirect
  `http://localhost:8765/callback`. The app's *Authorization Callback Domain* must be `localhost`.
- The access token is refreshed when it expires within 60 s. **Refresh tokens rotate**, so the newest one is always saved.
- Uses `GET /athlete/activities?after=…&per_page=200&page=N` until a short page arrives. Summary activities have
  no calories, so `calories` is `NULL` for Strava.
- Limits are about 100 requests per 15 minutes and 1000 per day. A 429 stops the stream, keeps the rows already
  written and leaves the cursor where it was.

### Google Health API v4 (replaces the Fitbit Web API)

- The legacy Fitbit Web API stopped being supported on 2026-09-30 and is turned off on 2026-10-30. Fitbit tokens do not
  carry over.
- OAuth uses a **Web application** client with PKCE (S256) plus the client secret, `access_type=offline` and
  `prompt=consent`. Three read-only scopes are requested: `googlehealth.activity_and_fitness.readonly`,
  `googlehealth.health_metrics_and_measurements.readonly` and `googlehealth.sleep.readonly`.
- After the token exchange, `GET /users/me/identity` is called. If it fails with `ACCOUNT_NOT_LINKED`, you get a message
  telling you to sign in to the Google Health (formerly Fitbit) app first.
- **Testing-mode consent screens expire refresh tokens after 7 days.** On `invalid_grant`, sync reports one `auth`
  error that tells you to re-run `fitness-hub auth google`.
- Streams (each one fails independently):

  | Stream | Request | Notes |
  |---|---|---|
  | `exercise`, `sleep` | `GET …/dataPoints` with `pageSize=25` | `exercise.interval.civil_start_time >= "<start>"`, `sleep.interval.civil_end_time >= "<start>"` |
  | `daily-resting-heart-rate`, `daily-heart-rate-variability`, `daily-oxygen-saturation`, `daily-respiratory-rate`, `daily-vo2-max` | `GET …/dataPoints` | `{snake}.date >= "<start>" AND {snake}.date < "<end+1>"` |
  | `weight`, `body-fat` | `GET …/dataPoints` | `{snake}.sample_time.physical_time >= "<start>T00:00:00Z"` |
  | `rollup:steps`, `rollup:distance`, `rollup:total-calories`, `rollup:floors`, `rollup:active-zone-minutes` | `POST …/dataPoints:dailyRollUp` | closed-open ranges, at most **14 days** per request for total calories and 90 days for the rest |

- URLs use kebab-case types (`body-fat`), filters use snake_case (`body_fat`) and JSON keys use camelCase (`bodyFat`).
  Filters only use `>=`, `<` and `AND`. int64 values arrive as strings and distances in millimetres; both are converted.
- **⚠ Unverified field names.** The value fields for **daily-oxygen-saturation**, **daily-respiratory-rate**,
  **daily-vo2-max**, **weight** and the **floors** rollup were not in the documentation available when this was built.
  The mapper tries a list of likely names, then falls back to the first numeric field and logs a warning naming the
  field it used. The raw payload is always stored, and running with `-v` logs one raw payload per type at DEBUG.
  Check these on your first live sync (see Limitations).

### Garmin Connect (unofficial, best-effort)

- Garmin's official APIs are only available to approved business partners, so this uses the community
  [`garminconnect`](https://pypi.org/project/garminconnect/) library (0.3.x, Python 3.12 or newer), which logs in like
  the website does.
- `fitness-hub auth garmin` asks for your email, password and MFA code once and saves only the session tokens.
  Later runs call `Garmin().login(token_dir)`. If login fails, the result is a `login` stream error.
- Streams:
  - `activities`: `get_activities_by_date(start, end)`.
  - `daily`: for each day, `get_stats` (steps, resting HR, stress, body battery, calories, distance, floors, SpO2,
    respiration), `get_sleep_data` (a session with stages plus `sleep_score`) and `get_hrv_data` (`hrv_rmssd_ms`;
    errors are ignored because many devices have no HRV). It pauses 0.4 s between days and stops at the first failure.
  - `body`: `get_body_composition(start, end)`; weights arrive in grams and are converted to kg.
- **CSV fallback:** in Garmin Connect on the web, go to Activities and choose **Export CSV**, then import the file with
  `import_garmin_csv(store, path, distance_unit="km"|"mi", timezone=None)`. It expects English column names, handles
  thousands separators and `h:mm:ss` times, and converts miles and feet when `distance_unit` is `mi`. Without a
  timezone, start times are stored without a zone.

## Cross-service de-duplication (at read time)

The same workout often shows up two or three times (watch → Garmin → Strava, and the same watch through Google Health).
`queries.dedupe_activities` groups activities from **different** sources whose start times are within **180 s** of each
other. It keeps one per group, preferring **garmin > google_health > strava > garmin_export**, and lists the other
sources in `also_recorded_by`. Source rows are never deleted. `list_activities` de-duplicates unless a `source` is given,
and `activity_summary` always does, so totals never count a workout twice. Daily metrics are **not** merged, because the
same metric can legitimately differ between sources.

`sport_family()` maps each provider's activity type to a common family (cycling, running, swimming, walking, hiking,
rowing, strength, skiing, yoga). That way an `activity_type="cycling"` filter matches Strava `Ride`, Garmin
`road_biking` and Google `OUTDOOR_BIKE`.

## Read-only SQL

`queries.run_select` accepts exactly one `SELECT`/`WITH` statement. As further protection, it installs a SQLite
authorizer that only allows reads (so `WITH … DELETE`, `PRAGMA` and `ATTACH` are refused even on a read-write
connection) and a 10-second time limit. The MCP server also runs it on a `mode=ro` + `PRAGMA query_only=ON` connection.

## Limitations

1. **Google Health access may be gated.** Google lists every Health API scope as *restricted* and, as of 2026-10, says
   it is *not onboarding new projects*. You may not be able to get credentials. A Google Takeout importer is future work.
2. **Google testing-mode tokens expire after 7 days.** Either publish the OAuth app (unverified, personal use) or re-run
   `fitness-hub auth google` every week.
3. **Garmin is unofficial.** Login can break without notice (Garmin tightened bot protection in March 2026), and
   `garminconnect` method names change between versions (`tests/test_garminconnect_api.py` guards the ones used).
   The CSV export importer is the fallback.
4. **Some Google field names are unverified:** SpO2, respiratory rate, VO2 max, weight and floors (see above).
   Mappings are best-effort until they have been checked against real payloads.
5. **Strava summaries have no calories**, and the 100-requests-per-15-minutes limit slows deep backfills.
6. **Garmin CSV times have no timezone.** Without `--timezone`, matching CSV activities against other sources is unreliable.
7. Google rollup responses are assumed to fit in one page (no `nextPageToken` handling for `dailyRollUp`).

## Future work

In priority order, from the plan:

1. Lock the Google mappings from real payloads, then add `heart-rate` and `daily-sleep-temperature-derivations`.
2. Strava detail fetch (calories, splits, streams) with a queue that respects rate limits, and Strava webhooks.
3. Google Takeout importer and Garmin bulk-export JSON importer.
4. Scheduling recipes: launchd, systemd timer, Dockerfile.
5. Remote MCP with authentication.
6. Token encryption at rest (OS keychain).
7. Derived training-load, sleep-regularity and HRV-baseline views.
8. Webhook-driven Google Health sync.
9. Schema migrations (`PRAGMA user_version`) before the first breaking schema change.
