# fitness-hub — Implementation Plan

> **Audience:** Claude Code. Build this repository end to end from this document.
> **Date written:** 2026-10-13. API facts below were verified against provider docs on that date;
> items marked **⚠ UNVERIFIED** must be checked against live responses before being trusted.
> A reference implementation exists (`fitness-hub.tar.gz`); if present in the workspace, treat it as a
> starting point to review and harden, not as gospel. If absent, build from this spec.

---

## 0. Instructions to Claude Code

1. **Repo target.** Default: one repository named `fitness-hub` containing everything. If the owner has designated
   different repo(s) (e.g. separate `fitness-hub-backend` and `fitness-hub-mcp`), see §14 for the split; otherwise
   do not split. Ask once at the start only if the target repo/remote is unclear; otherwise proceed.
2. **Work in phases (§12).** Commit at the end of each phase with a message that states what the phase added.
   Run the test suite before every commit. Never commit secrets (`.env`, `tokens.json`, `*.db`, `garmin-tokens/`).
3. **Never call real provider APIs in tests.** All HTTP is mocked with `httpx.MockTransport`. Real-account
   verification is a manual step for the owner (§11).
4. **Don't invent API facts.** Where this plan says ⚠ UNVERIFIED, implement defensively (store the raw payload,
   derive values best-effort, log a warning when the expected field is missing) and list it in the README.
5. **Definition of done:** every acceptance criterion in §13 passes, `pytest` is green, `fitness-hub --help` works,
   and the MCP server answers `initialize`, `tools/list` and a `tools/call` over a real stdio subprocess.

---

## 1. Goal and scope

Build a **local-first backend** that:

1. Authenticates to three services — **Strava**, **Garmin Connect**, **Google Health API** (successor to the Fitbit Web API).
2. **Ingests** their data on demand or on a schedule and **stores it locally** in SQLite (normalized columns + raw JSON).
3. Exposes the stored data to **Claude through an MCP server** (stdio by default, optional localhost HTTP).

### In scope
- Activities/workouts, daily health metrics (steps, resting HR, HRV, sleep score, SpO2, respiratory rate, VO2 max,
  active zone minutes, calories, distance, floors), sleep sessions with stages, body measurements (weight, body fat).
- Incremental sync with overlap, idempotent upserts, per-stream error isolation, rate-limit handling.
- Cross-service de-duplication of the same workout at **read time** (never delete source rows).
- CLI for auth/sync/status/import/serve/daemon.
- Read-only MCP tools plus a `sync_now` tool.

### Out of scope (v1)
- Multi-user support, a web UI, writing data back to any service, FIT/GPX stream storage, Strava/Google webhooks,
  cloud hosting, remote (internet-exposed) MCP with OAuth. (Some appear in §15 as future work.)

### Non-goals / principles
- **Local only.** No telemetry, no third-party services besides the three providers.
- **Raw data is never lost.** Every row keeps the provider payload in `raw`.
- **Read-only for Claude** except `sync_now`. SQL tool must be provably read-only.
- **Fail soft.** One broken stream must not stop the rest; errors are recorded, not swallowed.

---

## 2. Verified provider facts (the contract)

### 2.1 Strava — official API (stable)
- Auth: OAuth2 authorization code. Authorize `https://www.strava.com/oauth/authorize`; token `https://www.strava.com/oauth/token`.
- Scopes used: `read,activity:read_all,profile:read_all`.
- Redirect: app's **Authorization Callback Domain** must be `localhost`; we use `http://localhost:8765/callback`.
- Tokens: access token short-lived; response has `access_token`, `refresh_token`, `expires_at` (epoch s). Refresh via
  `POST /oauth/token` with `grant_type=refresh_token`. **Refresh tokens rotate** — always persist the new one.
- Data: `GET https://www.strava.com/api/v3/athlete/activities?after=<epoch>&per_page=200&page=N`.
  Summary fields used: `id, name, sport_type|type, start_date (UTC), start_date_local, elapsed_time, moving_time,
  distance (m), total_elevation_gain (m), average_heartrate, max_heartrate, average_watts`. `calories` is only on the
  *detailed* activity endpoint (not fetched in v1; stored `NULL`).
- Rate limits: ~100 requests / 15 min, 1000 / day. On HTTP 429, stop that stream and record the error.

### 2.2 Google Health API — successor to Fitbit Web API
- **Legacy Fitbit Web API: support ended 2026-09-30; turned off 2026-10-30.** Do not build on it. Fitbit OAuth tokens do
  not carry over; Google OAuth 2.0 is required.
- Base URL: `https://health.googleapis.com/v4` (a `v4beta` also exists; use `v4`).
- Auth: Google OAuth2, **client type "Web application"**, redirect `http://localhost:8765/callback` (must be registered).
  Use `access_type=offline`, `prompt=consent`, PKCE (S256) plus the client secret. Token endpoint
  `https://oauth2.googleapis.com/token`. Access token ≈ 1 h.
- **Consent screen in "Testing" status ⇒ refresh tokens expire after 7 days** → surface a clear "re-run auth" message on
  `invalid_grant`. Testing mode is capped at 100 test users (irrelevant for personal use).
- **Access caveat:** Google's docs state all scopes are *restricted* and (as of 2026-10) *"not onboarding new projects at
  this time"*. The owner may be unable to obtain credentials; Google Takeout import is the fallback (§15).
- The user must have signed into the Google Health (formerly Fitbit) app with that account before consent. After the token
  exchange call `GET /users/me/identity` → `{legacyUserId, healthUserId}`; HTTP 400 with reason `ACCOUNT_NOT_LINKED`
  means the account isn't linked yet — print an actionable message.
- Scopes (prefix `https://www.googleapis.com/auth/googlehealth`), read-only ones we request:
  - `.activity_and_fitness.readonly` — exercise, steps, distance, floors, total-calories, active-zone-minutes, daily-vo2-max, …
  - `.health_metrics_and_measurements.readonly` — heart-rate, daily-resting-heart-rate, daily-heart-rate-variability,
    daily-oxygen-saturation, daily-respiratory-rate, weight, body-fat, …
  - `.sleep.readonly` — sleep
- URL shape: `GET /users/me/dataTypes/{kebab-type}/dataPoints` (list), `…/dataPoints:reconcile` (GET, deduped stream),
  `POST …/dataPoints:rollUp`, `POST …/dataPoints:dailyRollUp`.
- **Data type naming:** kebab-case in URLs (`body-fat`), **snake_case in `filter`** (`body_fat`), camelCase as the JSON key of
  the data point (`bodyFat`). Using hyphens in a filter → 400 `INVALID_DATA_POINT_FILTER`.
- **Filter grammar (AIP-160, restricted):** only `>=` and `<`; only `AND` (no `OR`); physical time = RFC 3339 with `Z`/offset;
  civil time = `YYYY-MM-DD[THH:mm:ss]`; never mix physical and civil in one filter; the data type in the filter must match
  the path. Filter fields by record type:
  - Interval: `{t}.interval.start_time` | `{t}.interval.civil_start_time`
  - Sample: `{t}.sample_time.physical_time` | `{t}.sample_time.civil_time`
  - Daily: `{t}.date`
  - Session: exercise → `exercise.interval.civil_start_time`; sleep → `sleep.interval.end_time` / `sleep.interval.civil_end_time`
- **Pagination:** `pageSize`, `pageToken`, response `nextPageToken`. Default 1440; max 10 000; **`exercise` and `sleep` are
  capped at 25**. Results are ordered by interval start time **descending**.
- **Rollups:** `dailyRollUp` body = `{"range":{"start":<CivilDateTime>,"end":<CivilDateTime>},"windowSizeDays":1}` where
  `CivilDateTime = {"date":{"year","month","day"},"time":{"hours","minutes","seconds","nanos"}}`; range is **closed-open**.
  **Max range 14 days** for `calories-in-heart-rate-zone, heart-rate, active-minutes, total-calories`; **90 days** for the rest.
  Response `rollupDataPoints[]` with `civilStartTime` and a key named after the camelCase type, e.g.
  `{"steps":{"countSum":"8430"}}`, `{"distance":{"millimetersSum":"…"}}`, `{"totalCalories":{"kcalSum":1234.5}}`,
  `{"activeZoneMinutes":{"sumInCardioHeartZone","sumInPeakHeartZone","sumInFatBurnHeartZone"}}`.
- `int64` values arrive as **JSON strings** — always coerce. Distances/elevation are **millimetres**.
- Retry with exponential backoff on 429/5xx; Google explicitly warns against instant retries.
- Data is only available after the device syncs to the phone app (≈ every 15 min while the app is open).

**Confirmed field names (v4 reference):**
| Data type | Fields used |
|---|---|
| `exercise` | `interval{startTime,endTime,civilStartTime}`, `exerciseType` (enum e.g. `OUTDOOR_BIKE`, `RUNNING`), `displayName`, `activeDuration` ("1800s"), `metricsSummary{caloriesKcal, distanceMillimeters, steps, averageSpeedMillimetersPerSecond, averagePaceSecondsPerMeter, averageHeartRateBeatsPerMinute, activeZoneMinutes, elevationGainMillimeters}`; `name` = `users/{id}/dataTypes/exercise/dataPoints/{id}` |
| `sleep` | `interval{startTime,endTime,civilEndTime}`, `type` (CLASSIC/STAGES), `metadata{mainSleep,nap,processed}`, `summary{minutesAsleep,minutesAwake,minutesToFallAsleep,stagesSummary[{type:DEEP/LIGHT/REM/AWAKE,minutes,count}]}` |
| `dailyRestingHeartRate` | `date{y,m,d}`, `beatsPerMinute` |
| `dailyHeartRateVariability` | `date`, `averageHeartRateVariabilityMilliseconds`, `nonRemHeartRateBeatsPerMinute`, `entropy`, `deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds` |
| `bodyFat` | `sampleTime{physicalTime}`, `percentage` |
| `heartRate` (not synced v1) | `sampleTime`, `beatsPerMinute` |

**⚠ UNVERIFIED:** exact value field names for `dailyOxygenSaturation`, `dailyRespiratoryRate`, `dailyVo2Max`, and `weight`
(implementation uses a hint list + first-numeric fallback; raw payload is stored). Not exposed in the docs pages fetched.
First live run: log one raw payload per type at DEBUG and tighten the mapping.

Also note an inconsistency in Google's own docs: the `list` reference says sleep filters support `AND, OR`; the filter
guide says `OR` is unsupported. **Use `AND` only.**

### 2.3 Garmin Connect — unofficial
- Garmin's official Health/Activity APIs are **partner-only** (business approval); personal apps are rejected.
- Use the community library **`garminconnect`** (PyPI, Python ≥ 3.12 in recent versions) — it logs in like the website.
  Reports exist of breakage after Garmin tightened bot protection in March 2026; **treat as best-effort** and keep the
  CSV importer working as the fallback.
- Auth: interactive email/password/MFA once → session tokens saved to a token directory (default `~/.fitness-hub/garmin-tokens`).
  **Never persist the password.** Later runs use `Garmin().login(token_dir)`.
- Methods used (⚠ verify names against the installed version): `get_activities_by_date(start, end)`, `get_stats(date)`,
  `get_sleep_data(date)`, `get_hrv_data(date)`, `get_body_composition(start, end)`.
- Field notes: activity `startTimeGMT`/`startTimeLocal` are `"YYYY-MM-DD HH:MM:SS"` (no zone); `activityType.typeKey`;
  weights in **grams**; sleep timestamps in **epoch ms**; daily `totalSteps`, `restingHeartRate`, `averageStressLevel`,
  `bodyBatteryHighestValue`; HRV `hrvSummary.lastNightAvg`; sleep score `dailySleepDTO.sleepScores.overall.value`.
- Per-day loops are many calls: pause ~0.4 s between days, stop on the first failure (likely throttling) and resume from the cursor.
- CSV fallback: Garmin Connect web → Activities → **Export CSV** (English columns: `Activity Type, Date, Title, Distance,
  Calories, Time, Avg HR, Max HR, Total Ascent, Avg Power`). Distance unit follows the account (km or mi) → CLI flag.

---

## 3. Architecture

```
                 ┌────────────── fitness-hub (Python ≥3.11) ──────────────┐
  Strava API ───►│ providers/strava.py ──┐                                │
  Garmin (unoff)►│ providers/garmin.py ──┼─► sync.py ─► db.Store ─► SQLite │
  Google Health ►│ providers/google_health.py ┘            ▲        (WAL)  │
                 │                                          │ read-only    │
                 │ cli.py  (auth/sync/status/import/run)    │              │
                 │ mcp_server.py ─► queries.py ─────────────┘              │
                 └───────────────┬─────────────────────────────────────────┘
                                 │ stdio (default) / streamable-HTTP (localhost)
                              Claude Code / Claude Desktop
```

Layers and rules:
- **providers/** know the remote API and map payloads → normalized dicts. *Pure mapper functions* are separate from I/O
  so they can be unit-tested. Providers never touch SQL except via `Store` methods.
- **db.py** owns schema and upserts. All writes are `INSERT … ON CONFLICT DO UPDATE` ⇒ idempotent.
- **sync.py** builds the date window, iterates providers, logs, never raises on provider failure.
- **queries.py** is read-side only (works on any `sqlite3.Connection`), incl. cross-source de-duplication.
- **mcp_server.py** opens **read-only** connections (`file:…?mode=ro` + `PRAGMA query_only=ON`) per call.
- **config.py** reads env vars, with `.env` support (no extra dependency). **auth.py** holds token storage + local OAuth redirect.

Tech choices: `httpx` (sync client), `mcp>=1.30,<2` (`mcp.server.fastmcp.FastMCP`; pinned below 2.x because the 2.x API
was not reviewed), stdlib `sqlite3`, `argparse`. Optional extra `garmin` → `garminconnect`. Dev extra → `pytest`. Build with `hatchling`.

---

## 4. Repository layout

```
fitness-hub/
├── pyproject.toml
├── README.md                  # user-facing setup (per-provider steps, Claude config, caveats)
├── PLAN.md                    # this file
├── .env.example
├── .gitignore                 # .venv, __pycache__, .env, *.db*, tokens.json, garmin-tokens/
├── src/fitness_hub/
│   ├── __init__.py            # __version__
│   ├── config.py              # Settings dataclass, .env loader
│   ├── db.py                  # SCHEMA, Store (upserts, sync_state), open_readonly()
│   ├── http.py                # request_json with retry/backoff, RateLimited, ApiError
│   ├── auth.py                # TokenStore(0600), PKCE, local redirect server, expires_soon
│   ├── sync.py                # build_providers, run_sync
│   ├── queries.py             # list/summary/daily/sleep/body/run_select + dedupe
│   ├── mcp_server.py          # create_server(settings, host, port)
│   ├── cli.py                 # argparse commands
│   └── providers/
│       ├── __init__.py        # SyncResult, SyncWindow
│       ├── strava.py
│       ├── garmin.py          # provider + CSV importer
│       └── google_health.py
└── tests/
    ├── conftest.py            # settings/store/tokens fixtures (tmp_path)
    ├── test_providers.py
    └── test_queries_and_mcp.py
```

---

## 5. Configuration

Environment variables (also read from `$FITNESS_HUB_HOME/.env` then `./.env`; real env wins):

| Var | Default | Purpose |
|---|---|---|
| `FITNESS_HUB_HOME` | `~/.fitness-hub` | DB, tokens, Garmin tokens, `.env` |
| `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` | – | Strava app |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | – | Google "Web application" OAuth client |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | – | Optional, only to skip prompts during `auth garmin` |
| `OAUTH_PORT` | `8765` | Local redirect port |
| `BACKFILL_DAYS` | `365` | First-run history window |
| `LOOKBACK_DAYS` | `7` | Overlap re-fetched on every incremental run |

Files under `FITNESS_HUB_HOME`: `fitness.db` (+ `-wal`/`-shm`), `tokens.json` (mode 0600, written atomically via temp+`os.replace`),
`garmin-tokens/`. The home directory should be created with default perms; tokens file must be 0600.

---

## 6. Database schema (SQLite, WAL)

Timestamps are **UTC ISO-8601 text**; dates are `YYYY-MM-DD` on the athlete's **local** clock. All tables carry `updated_at`.

```sql
activities(
  source TEXT, source_id TEXT,            -- PK (source, source_id)
  start_time TEXT NOT NULL, local_date TEXT NOT NULL,
  activity_type TEXT, name TEXT,
  duration_s REAL, moving_s REAL, distance_m REAL, elevation_gain_m REAL,
  avg_hr REAL, max_hr REAL, avg_power_w REAL, calories REAL,
  raw TEXT NOT NULL, updated_at TEXT NOT NULL)            -- idx on local_date

daily_metrics(source, date, metric, value REAL, raw TEXT, updated_at)   -- PK (source,date,metric); idx on date
sleep_sessions(source, source_id, start_time, end_time, local_date,     -- local_date = date sleep ENDED
  minutes_asleep, minutes_awake, minutes_deep, minutes_light, minutes_rem,
  is_main INTEGER, raw, updated_at)                                     -- PK (source,source_id); idx on local_date
body_measurements(source, ts, metric, value REAL, raw, updated_at)      -- PK (source,ts,metric)
sync_state(source, stream, cursor, last_attempt, last_success, last_error, rows_upserted)  -- PK (source,stream)
```

**Metric vocabulary** (`daily_metrics.metric`): `steps, distance_m, total_calories_kcal, active_calories_kcal, floors,
active_zone_minutes, resting_hr, hrv_rmssd_ms, sleep_score, avg_stress, body_battery_high, body_battery_low, spo2_pct,
respiratory_rate_bpm, vo2_max`. **Body:** `weight_kg, body_fat_pct`. Keep names stable; they are part of the MCP contract.

**Source names:** `strava`, `garmin`, `google_health`, `garmin_export` (CSV import).

`sync_state.record_sync` semantics: on success set `last_success`, keep/advance `cursor`; on error keep the previous
`cursor` and `last_success`, set `last_error`. A `NULL` cursor argument must never erase a stored cursor.

---

## 7. Sync algorithm

`run_sync(settings, sources=None, since=None, full=False)`:
1. `window = SyncWindow(start = since or today − BACKFILL_DAYS, end = today)`.
2. For each requested source: skip with a log line if not authorized (`configured()` false).
3. Call `provider.sync(store, window, full)`; log per-stream row counts and errors. Return all `SyncResult`s.
4. CLI exit code is 1 if any stream errored.

**Per-stream start date:** `window.start` if `full` or no cursor; else `cursor − LOOKBACK_DAYS` (clamped to `window.end`).
Cursors: Strava `activities` = max `start_time` seen; day-based streams = `window.end` (Garmin `daily` = last successfully
synced day, so a throttled run resumes where it stopped).

### Strava
- Page `per_page=200` until a short page. Upsert via `map_activity`. Refresh token when `expires_at < now+60`, persist rotated token.
- 429 ⇒ record `rate limited`, keep rows already written, return.

### Google Health streams (each wrapped so one failure doesn't stop others)
| Stream | Call | Filter / body | Page |
|---|---|---|---|
| `exercise` | list | `exercise.interval.civil_start_time >= "<start>"` | 25 |
| `sleep` | list | `sleep.interval.civil_end_time >= "<start>"` | 25 |
| daily types: `daily-resting-heart-rate`, `daily-heart-rate-variability`, `daily-oxygen-saturation`, `daily-respiratory-rate`, `daily-vo2-max` | list | `{snake}.date >= "<start>" AND {snake}.date < "<end+1>"` | default |
| `weight`, `body-fat` | list | `{snake}.sample_time.physical_time >= "<start>T00:00:00Z"` | default |
| rollups: `steps, distance, total-calories, floors, active-zone-minutes` | `POST :dailyRollUp` | range chunked to **≤14 d** for `total-calories`, **≤90 d** otherwise; `windowSizeDays=1`; end is exclusive | n/a |

Rollup value mapping: steps `countSum`; distance `millimetersSum × 0.001` → `distance_m`; total calories `kcalSum`;
active zone minutes = sum of the three `sumIn*HeartZone`; floors `countSum` (⚠ UNVERIFIED) with first-numeric fallback.
Rollup streams use cursor key `rollup:<type>`.

Exercise mapping: `duration_s = end − start`; `moving_s = activeDuration` ("…s"); mm → m; `avg_hr` from
`averageHeartRateBeatsPerMinute`; `source_id` = last path segment of `name` (fallback: start time). Sleep: stage minutes from
`stagesSummary`; `is_main = metadata.mainSleep`; `local_date` from `civilEndTime`.

### Garmin
- `activities`: `get_activities_by_date(start, end)`.
- `daily`: loop day by day: `get_stats`, `get_sleep_data`, optional `get_hrv_data` (swallow errors — absent on many devices);
  write metrics + sleep; sleep 0.4 s; on first exception record error with the day and stop.
- `body`: `get_body_composition(start, end)` → `dateWeightList` (grams → kg).
- Login failure ⇒ stream `login` error: "run `fitness-hub auth garmin`".

### HTTP helper (`http.request_json`)
Retry on 429/502/503/504 with exponential backoff (2 s doubling, ≤4 retries, honoring numeric `Retry-After`). If the required
wait exceeds 120 s raise `RateLimited` immediately (don't block a sync for a 15-minute Strava window). ≥400 otherwise ⇒
`ApiError(status, body, url)`. Empty body ⇒ `None`. Sleep function is injectable for tests.

---

## 8. Cross-service de-duplication (read time only)

Same workout often appears 2–3 times (Garmin watch → Strava; the same watch data via Google Health).
- Cluster activities whose `start_time` differ by ≤ **180 s** and that come from **different sources**.
- Keep one per cluster by priority **garmin > google_health > strava > garmin_export**; add `also_recorded_by: [sources]`.
- Timestamps parsed with `fromisoformat` (`Z` → `+00:00`); naive timestamps assumed UTC (CSV export is local-time → poor matching, documented).
- Applies to `list_activities` (unless an explicit `source` is passed) and always to `activity_summary` so totals never double count.
- Daily metrics are **not** merged: the same metric can legitimately differ per source; tools return `source` with each row.

---

## 9. MCP server

Name `fitness-hub`; ships `instructions` describing units, dedupe, metric names and tables. Transport: **stdio** default;
`streamable-http` on `127.0.0.1:8800/mcp` optionally (no auth — localhost only, say so in logs/README).

| Tool | Signature | Behavior |
|---|---|---|
| `sync_status` | `()` | `[{row_counts}, ...sync_state rows]` |
| `list_activities` | `start_date?, end_date?, activity_type?, source?, limit=50` | newest first, loose `LIKE` on type, limit ≤ 500, dedupe unless `source` set |
| `get_activity` | `source, source_id, include_raw=false` | one row or `null` |
| `activity_summary` | `start_date, end_date, group_by=week|month|day|type, activity_type?` | count, distance_km, moving_hours, elevation_gain_m, calories, avg_of_avg_hr; weeks start Monday; invalid `group_by` ⇒ error |
| `get_daily_metrics` | `start_date?, end_date?, metrics?[], source?` | default last 30 d; rows `{date, metric, source, value}` |
| `get_sleep` | `start_date?, end_date?, main_only=true, source?` | default last 30 d |
| `get_body_measurements` | `start_date?, end_date?, metric?` | default last 90 d |
| `query_sql` | `sql, limit=200` | **single** `SELECT`/`WITH` only; read-only connection; limit ≤ 1000; returns `{columns, rows, truncated}` |
| `sync_now` | `sources?[], full=false` | runs `run_sync`; returns `{log[], ok}` (blocking; warn about rate limits for `full`) |

Safety: `query_sql` rejects anything not starting with `select|with`, anything containing a second statement, and relies on
`mode=ro` + `PRAGMA query_only=ON` as defense in depth. Tokens are never in the DB, so they cannot be selected.
Tool errors must surface as MCP errors (`isError=true`), not crashes. Create the DB file/schema if missing before opening read-only.

---

## 10. CLI

```
fitness-hub auth {strava|google|garmin}
fitness-hub sync [--source strava|garmin|google_health]... [--since YYYY-MM-DD] [--full]
fitness-hub status
fitness-hub import-garmin-csv PATH [--distance-unit km|mi]
fitness-hub serve-mcp [--transport stdio|http] [--host 127.0.0.1] [--port 8800]
fitness-hub run [--interval-minutes 60] [--http] [--host …] [--port …]     # daemon
```
- `auth strava|google`: build URL (random `state`; Google adds PKCE), open browser + print URL, local `HTTPServer` on
  `127.0.0.1:OAUTH_PORT` validates `state`, exchanges the code, saves tokens. 5-minute timeout. Google then calls `/users/me/identity`.
- `run`: loop `run_sync` every N minutes inside try/except (daemon must survive errors); with `--http` run the sync loop in a
  daemon thread and the MCP HTTP server on the main thread.
- `status`: print each `sync_state` row (with errors) and table counts.

---

## 11. Manual verification checklist (owner, real accounts)

1. `pip install -e '.[garmin,dev]' && pytest` → green.
2. Strava: create API app, `fitness-hub auth strava`, `fitness-hub sync --source strava --since <30 days ago>`, `fitness-hub status`.
3. Google: Cloud project → enable Google Health API → Web client (redirect `http://localhost:8765/callback`) → consent screen
   Testing + your account as test user + the 3 read scopes → `fitness-hub auth google` → sync. **On first run, inspect `raw` for
   `daily_oxygen_saturation`, `daily_respiratory_rate`, `daily_vo2_max`, `weight` and fix mappings (⚠ UNVERIFIED items).**
4. Garmin: `fitness-hub auth garmin`; sync a 7-day window first. If login fails, use the CSV export path.
5. `claude mcp add fitness-hub -- $(pwd)/.venv/bin/fitness-hub serve-mcp`, then ask Claude: *"What was my weekly cycling volume
   for the last 8 weeks?"* and *"What's my resting HR trend this month?"*.
6. Confirm the same ride recorded by two services is counted once in `activity_summary`.

---

## 12. Phased build

**Phase 1 — Foundation:** `pyproject.toml`, `config.py`, `db.py` (schema + all upserts + `sync_state` + read-only opener),
`http.py`, `auth.py`, `.gitignore`, `.env.example`, `conftest.py`. *Tests:* upsert idempotency, `record_sync` keeps cursor on error.

**Phase 2 — Strava:** provider + `authorize`. *Tests:* token refresh and rotation persisted, two-page pagination, incremental
re-run idempotent, 429 recorded as error without raising.

**Phase 3 — Google Health:** pure mappers, `authorize` (+identity check), provider with all streams and chunked rollups.
*Tests:* mapper unit tests, end-to-end with MockTransport (pagination token, filter strings, civil date filters, rollup chunk
counts: 43 days ⇒ 4 total-calories requests / 1 steps request), one failing stream (403) doesn't block others,
`invalid_grant` ⇒ actionable error.

**Phase 4 — Garmin:** provider with injected fake client, mappers, CSV importer, `authorize`. *Tests:* fake-client end-to-end
(activities, daily metrics, sleep, body kg conversion), CSV import (commas in numbers, `h:mm:ss`, unit conversion).

**Phase 5 — Read side + MCP:** `queries.py` (incl. dedupe), `mcp_server.py`. *Tests:* dedupe prefers device source and sets
`also_recorded_by`; summary doesn't double count; `run_select` rejects `DELETE`, multi-statement and `PRAGMA`; MCP tools listed
and callable in-process **and** via a real stdio subprocess (`mcp.client.stdio`).

**Phase 6 — CLI, sync orchestration, docs:** `sync.py`, `cli.py`, README, smoke run (`status`, `sync` with nothing authorized
prints "skipped" and exits 0).

---

## 13. Acceptance criteria

- [ ] `pytest` passes with **no network access**.
- [ ] Re-running `sync` twice produces identical row counts (idempotent) and never duplicates rows.
- [ ] Killing a sync mid-way leaves a consistent DB; the next run resumes (cursor semantics in §6/§7).
- [ ] One failing stream is reported in `status`/`sync_status` while the others still complete.
- [ ] `tokens.json` is mode `0600`; Garmin password is never written to disk; no secrets in logs.
- [ ] `query_sql` cannot modify the DB (tested with DDL/DML/PRAGMA/multi-statement).
- [ ] MCP server works over real stdio: `initialize`, `tools/list` (9 tools), `tools/call query_sql`.
- [ ] Dedupe: Garmin+Strava same-ride fixture yields 1 activity; totals not doubled.
- [ ] Google rollup requests respect 14/90-day limits; filters use snake_case types and only `>=`, `<`, `AND`.
- [ ] README documents per-provider setup, the 7-day Google testing-token expiry, Garmin fragility, HTTP-mode no-auth warning.

---

## 14. If the owner uses multiple repos

Only do this if explicitly told which repos. Suggested split:
- **`fitness-hub-core`** (library): `config, db, http, auth, queries, providers/*, sync` + tests for them.
- **`fitness-hub-mcp`** (app): `mcp_server.py`, `cli.py` (serve-mcp/run) depending on `fitness-hub-core`; README with Claude config.
Keep the `fitness_hub` import package name stable, version both together, and put the shared schema in core only.
CI in each repo runs `pytest`. Do not duplicate provider code.

---

## 15. Known risks and future work (record in README "Limitations")

**Risks**
1. **Google Health access gating** (restricted scopes, not onboarding new projects) — may block the Fitbit/Pixel path.
2. **Google testing-mode token expiry** (7 days) — mitigate by publishing the app (unverified, personal) or accepting weekly re-auth.
3. **Garmin unofficial API** — may break without notice; CSV fallback is the safety net. `garminconnect` method names/signatures drift between versions.
4. **Unverified Google field names** (§2.2) — mapping is best-effort until validated against live data.
5. **Strava summary lacks calories** and the 100/15-min limit constrains deep backfills.
6. **Timezone ambiguity** for the Garmin CSV (local time without zone) — de-dup against other sources is unreliable.

**Future work (priority order)**
1. Validate and lock Google mappings from real payloads; add `heart-rate` (14-day rollup limit) and `daily-sleep-temperature-derivations`.
2. Strava detail fetch for calories/splits/streams with a rate-limit-aware queue; Strava webhooks.
3. Google **Takeout importer** (fallback if API access is unavailable) and Garmin **bulk-export JSON importer**.
4. Scheduling recipes: `launchd` plist, `systemd` timer, Dockerfile.
5. Remote MCP with authentication (bearer token / OAuth) behind a reverse proxy; claude.ai-compatible deployment.
6. Optional token encryption at rest (OS keychain via `keyring`).
7. Derived tables/views: weekly training load, CTL/ATL (needs power/HR zones), sleep regularity, HRV baselines.
8. Webhook-driven incremental sync for Google Health (supports webhooks per data type) instead of polling.
9. Schema migrations (`PRAGMA user_version`) before the first breaking schema change.
