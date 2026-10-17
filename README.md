# fitness-hub (performance-tracker-backend)

A **local-first** hub for your training and health data. It pulls workouts, daily metrics, sleep and body
measurements from **Strava**, **Garmin Connect** and the **Google Health API** (Fitbit/Pixel; the successor to the
Fitbit Web API) into a SQLite database on your machine. It then lets **Claude** read that data through an
**MCP server**, so you can ask things like *"What was my weekly cycling volume for the last 8 weeks?"*

- Your data stays local: no telemetry, and nothing is contacted except the three providers.
- Raw provider payloads are kept, and re-syncing is idempotent.
- Claude can only read, apart from `sync_now`. The SQL tool cannot change the database.
- The same ride recorded by Garmin, Strava and Google is counted once.

This repository holds all of the Python side: storage, provider sync, the `fitness-hub` CLI and the MCP server. It also has
the read API for the web UI (`fitness-hub serve-api`). The UI itself lives in
[performance-tracker-ui](https://github.com/iamthanh/performance-tracker-ui). The design spec is [PLAN.md](PLAN.md).

## Install

You need Python 3.11 or newer. Python **3.12 or newer is required for the Garmin provider**, because `garminconnect`
needs it.

```bash
git clone https://github.com/iamthanh/performance-tracker-backend.git
cd performance-tracker-backend
python3.12 -m venv .venv
.venv/bin/pip install -e '.[garmin,dev]'
.venv/bin/pytest            # optional: the whole suite runs offline
.venv/bin/fitness-hub --help
```

Without cloning: `pip install 'fitness-hub[garmin] @ git+https://github.com/iamthanh/performance-tracker-backend.git'`.

## Configure

Settings are read from environment variables, then from `~/.fitness-hub/.env` (`$FITNESS_HUB_HOME/.env`), then from
`./.env`. Real environment variables win. Start from [`.env.example`](.env.example):

```bash
mkdir -p ~/.fitness-hub && cp .env.example ~/.fitness-hub/.env && chmod 600 ~/.fitness-hub/.env
```

| Variable | Default | Purpose |
|---|---|---|
| `FITNESS_HUB_HOME` | `~/.fitness-hub` | Database (`fitness.db`), `tokens.json`, `garmin-tokens/` |
| `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` | – | Your Strava API application |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | – | Google OAuth client ("Web application") |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | – | Optional; skip the prompts of `auth garmin` (the password is never saved by fitness-hub) |
| `OAUTH_PORT` | `8765` | Local OAuth redirect port; must match the registered redirect URI |
| `BACKFILL_DAYS` | `365` | History fetched on the first sync |
| `LOOKBACK_DAYS` | `7` | Overlap re-fetched on every sync, to pick up late edits |
| `GARMIN_ACTIVITIES` | `false` | Also sync workouts from Garmin. Off by default: workouts come from Strava, Garmin supplies daily health data |

Files in `FITNESS_HUB_HOME`:

- `fitness.db` (with `-wal`/`-shm`): created owner-only (0600) because it holds health data.
- `tokens.json`: OAuth tokens, mode 0600, written atomically (temp file + `os.replace`). Tokens are never stored in the
  database and never logged.
- `garmin-tokens/garmin_tokens.json`: Garmin session tokens (0600 in a 0700 directory). Your Garmin password is never
  written anywhere.
- `sync.lock`: stops two syncs (for example the daemon and an MCP `sync_now`) from running at once.

## Connect your services

You only need to authorize the services you use. Sources that are not authorized are skipped.

### Strava bulk export (no API access needed)

Since June 2026 Strava's API needs a paid Strava subscription. Without one, import your history from Strava's own
export instead. It includes every activity's original file, so you get per-second power, heart rate, cadence and GPS:

1. On strava.com go to **Settings → My Account → Download or Delete Your Account → Request your archive**. The
   email with the download link usually comes within a few hours.
2. Unzip it and run:

```bash
fitness-hub import-strava-export ~/Downloads/export_12345678 --timezone America/Los_Angeles
```

Activities are stored as source `strava` under their Strava activity ID. Each original file (`.fit`, `.gpx`, `.tcx`
or the Strava app's `.json`, gzipped or not) is parsed into `activity_streams`, in parallel (`--workers`, default up
to 8). About 1,700 activities take a couple of minutes.

- **Local dates** come from the FIT file's own local time. Otherwise `--timezone` is used (GPX/TCX files, and Zwift
  rides, whose FIT local time is not a real time zone). Without either, the date stored by an earlier import is kept,
  and a new activity gets its UTC date.
- **Imperial accounts:** pass `--distance-unit mi`. It only matters for rows whose detailed distance is missing.
- **Re-running is safe and quick:** files whose streams are already stored are skipped unless you pass `--force`. To
  pick up new activities, request a fresh archive and import it again.
- **Activities synced from the Strava API are kept as they are** (their payload is richer than the CSV); the import only
  adds their streams.

### Training analysis (TSS, NP, FTP, fitness/fatigue/form)

The import runs the analysis at the end; `fitness-hub analyze` runs it again (only new or changed activities;
`--full` for all). About 1,700 activities take ~15 s. After upgrading from a version without the analysis, run
`fitness-hub analyze` once: until then `training_load` is empty. For each activity with streams it computes:

- normalized power, intensity factor, TSS, variability index, efficiency factor (NP / avg HR), work (kJ), Pw:HR
  decoupling, time in Coggan power zones and Friel HR zones, and the best average power for 1 s to 5 h;
- TSS from power for rides (`seconds × NP × IF / (FTP × 3600) × 100`). Otherwise (runs, rides without a working power
  meter) an HR-based TSS: Banister's TRIMP, scaled so that an hour at threshold heart rate scores 100. On rides that
  have both, it agrees with power TSS at the median.

How the streams are read:

- They are put on a 1 Hz timer clock. Gaps of up to 5 s are filled with the previous value, and so are longer gaps
  covered at 1 m/s or more (watches that record every few seconds). Other gaps are pauses and are left out.
- Missing power counts as 0 W. Heart rate below 30 bpm (a strap that lost contact) or above 220 bpm (sensor spikes)
  counts as missing.
- Power is only used when at least 25 % of the ride has power above 0 W, so a dying power meter does not set the TSS.
- Peak powers and best 20-minute values never join efforts across a pause.
- Normalized power is the standard 30 s / 4th-power calculation. On the rides tested it was a median of 1.6 W below
  the Wahoo head unit's own value.

**Thresholds.** Detected automatically from rides in the trailing 365 days:

- FTP: estimated from the whole power curve, not a single effort.
  - Critical power is fitted to your best 3- to 30-minute efforts (work = CP × time + W′). The fit needs bests from 7
    minutes or less up to at least 20 minutes, and a W′ between 5 and 40 kJ. A flat curve, from steady riding without
    short all-out efforts, gives no sensible fit. Without one, FTP is 95 % of the best 20 minutes.
  - It is never below your best hour, since an hour at X W proves an FTP of at least X.
  - It rises only on a day you set a new best at some duration, never because an old effort aged out. It falls by at
    most 1 % a week, so when a big effort ages out of the 365-day window FTP eases down instead of dropping overnight.
    The flip side: after a long break it lags. Set your FTP yourself if you know it has dropped.
  - Your own value holds until the automatic one *rises* above it with a new best; an automatic value easing down
    from higher up never overrides it.
- Threshold HR: the best 20-minute heart rate.
- Max HR: the third-highest best-30-second heart rate of any activity, so one or two noisy sessions don't set it.
- The first values use your first four weeks of rides.
- Resting HR, used by the HR-based TSS, defaults to 50 bpm. If max HR is unknown or not above threshold HR, threshold
  HR is taken as 92 % of max.

Set your own from a date on:

```bash
fitness-hub thresholds set ftp 250 --from 2026-09-01 --note "ramp test"
fitness-hub thresholds set threshold-hr 168 --from 2026-09-01
fitness-hub thresholds set resting-hr 48 --from 2026-01-01
fitness-hub thresholds list
fitness-hub thresholds delete ftp --from 2026-09-01
```

A manual value holds until your next manual value, unless the automatic value later rises above it (a new best
effort). Before the first value, the first one applies. Changing a threshold re-scores the affected activities from
their stored results, without reading the streams again.

**Fitness, fatigue, form** (MCP `training_load`): daily TSS, CTL (42-day exponentially weighted average), ATL (7-day)
and TSB, the previous day's CTL − ATL, as on TrainingPeaks' Performance Management Chart. Only activities with
imported streams have a TSS; each day also reports `unscored` workouts, so you can tell when the load is understated
(for example, new rides that have not been imported yet).

### Importing a single file (web UI)

The Activities page's **Import workout** button uploads `.fit`, `.gpx` or `.tcx` files (also gzipped), up to 50 MB
each (60 MB once unzipped), through `POST /api/activities/import`. Imports run one at a time.

- **One workout per ride.** Each file becomes an `upload` activity. Importing the same recording again, gzipped or
  not, or another file of the same ride (say a GPX after the FIT), updates that workout instead of adding a
  second copy that every total would count again. A name or sport you gave it is kept unless you give a new one.
- **Stored and analysed:** the samples go into `activity_streams`, and the training analysis runs straight away
  (NP, TSS, zones, power curve, FTP). If the analysis fails, the workout is still stored and the next analysis
  retries it. A file whose samples cannot be stored leaves nothing behind.
- **Totals** come from the FIT session where there is one, and a multisport file's legs are added up, as sport
  `Multisport`. Otherwise they are worked out from the samples:
  - duration;
  - distance (the distance channel, or the GPS track ignoring jitter under 3 m);
  - elevation gain (ignoring wiggles under 2 m);
  - heart rate and power.
  
  Moving time for GPX and TCX is the time the analysis keeps (pauses left out); for FIT it is the device's timer
  time. On 11 real Strava-export files these came within about 1 % of Strava's own figures. TCX laps and calories
  are not read.
- **Local date:** the file's own clock (most FIT files), else the time zone the browser sends.
- **Name and sport:** taken from the file, or set in the dialog. Without a name, it is "Morning Ride",
  "Evening Run", etc.
- **Refused:**
  - a recording spanning more than 48 h (a device clock that jumped);
  - a file with no samples (a course or route);
  - anything that does not parse.
- **Duplicates from other services:** a workout already stored from another service (e.g. a later Strava export) is
  merged in the lists, as for any duplicate, and the import says so.

### Nutrition (logged by you)

Meals and fueling during workouts are logged in the web UI (or through the API) and stored in `nutrition_entries`;
saved foods (`foods`) make it one tap. There is no importer yet.

- **Each entry keeps its own nutrient totals**, copied when it is logged, so editing or deleting a saved food never
  changes your history. Logging from a saved food fills in its nutrients × servings; anything you type in wins.
- **Workout fueling** is an entry with meal `during_workout` linked to the workout, optionally with the time into it.
  For each workout: carbs (g/h), fluid (ml/h), sodium (mg/h) and caffeine taken during it, per hour of moving time.
- **Energy out.** A workout's kcal is its recorded calories (from whichever service recorded them), else its work in
  kJ (for cycling, kJ of work ≈ kcal burned). A day's total is your watch's total calories (Garmin, else Google
  Health), once synced; until then only workout kcal is known. Today's totals are flagged `partial`.
- **Carbohydrate burned** is a rough estimate from the workout's intensity factor: about a third of the energy at a
  recovery pace, two thirds at IF 0.75, nearly all near threshold (the crossover concept, for a rider who has eaten;
  fasted riders burn more fat). It errs high, toward eating more, and is for comparing with what you ate, not a
  measurement.
- **Drinks during a workout are bottles with a recipe.** You give the number of bottles and what one bottle holds:
  - water (ml);
  - table salt (g; 1 g ≈ 393 mg of sodium);
  - maltodextrin and sucrose (g);
  - any other ingredients, each with its own carbs, sodium and caffeine (e.g. sodium citrate, a caffeine powder).
  
  The entry keeps the recipe (`kind = drink`, `recipe` JSON) and stores the totals: fluid, carbs, sugars, sodium,
  caffeine and 4 kcal per gram of carbs. A workout's fueling adds the bottles' ingredients up and gives the
  glucose:fructose split: maltodextrin is glucose, sucrose is half glucose and half fructose.
- **Body weight before and after** a workout (`workout_weights`, `PUT /api/activities/{source}/{id}/weight`) gives
  the weight change, % of body weight, and the sweat rate: weight lost plus fluid drunk, per hour of elapsed time.
- **Editing an entry** keeps its nutrients unless you send new ones; changing the servings of a saved-food entry
  scales them, and pointing it at another food refills them from that food. An entry linked to a workout counts for
  that workout whichever service's copy of it you linked.

### Strava API (needs a Strava subscription)

1. Create an API application at <https://www.strava.com/settings/api>. Set **Authorization Callback Domain** to
   `localhost`.
2. Put `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET` in your `.env`.
3. Run `fitness-hub auth strava`. A browser opens; approve access and keep the "view private activities" box
   checked. The redirect goes to `http://localhost:8765/callback`, where fitness-hub is listening for up to 5 minutes.
   On a machine with no browser, add `--no-browser` and open the printed URL yourself.
4. Run `fitness-hub sync --source strava --since 2026-09-14` to start with a small window.

Strava allows about 100 requests every 15 minutes. A large backfill can hit that limit. When it does, the sync records
`rate limited`, keeps what it already fetched, and the next sync continues. Strava activity summaries include no
calories.

### Google Health API (Fitbit / Pixel Watch)

> **Access caveat.** Google lists every Google Health API scope as *restricted* and, as of October 2026, says it is
> *not onboarding new projects*. You may not be able to get this working yet. The legacy Fitbit Web API is turned off
> on 2026-10-30 and its tokens do not carry over.

1. Install the **Google Health** app (formerly Fitbit) and sign in with the Google account you will authorize. Let your
   device sync. Data only appears after the phone app syncs.
2. In the Google Cloud console, create a project and **enable the Google Health API**.
3. Configure the **OAuth consent screen**: user type External, status **Testing**, add your own account as a **test
   user**, and add these three scopes:
   - `https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly`
   - `https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly`
   - `https://www.googleapis.com/auth/googlehealth.sleep.readonly`
4. Create an **OAuth client ID** of type **Web application**, with the authorized redirect URI
   `http://localhost:8765/callback`. Put `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in your `.env`.
5. Run `fitness-hub auth google`, then `fitness-hub sync --source google_health`.

> **⚠ Refresh tokens expire after 7 days while the consent screen is in "Testing" status.** When that happens, sync
> reports `invalid_grant` and tells you to run `fitness-hub auth google` again. To avoid weekly re-auth, publish the
> OAuth app ("In production"; it can stay unverified for personal use).

If auth says the account is **not linked**, sign in to the Google Health app with that account, sync, and try again.

**Check after your first sync.** The value fields for SpO2, respiratory rate, VO2 max, weight and floors were not in
Google's documentation when this was written. fitness-hub guesses them, logs a warning naming the field it used, and
always stores the raw payload. Run `fitness-hub -v sync --source google_health` once, then compare the values against
the app. For example:

```bash
sqlite3 ~/.fitness-hub/fitness.db "SELECT date, metric, value, raw FROM daily_metrics WHERE source='google_health' AND metric IN ('spo2_pct','respiratory_rate_bpm','vo2_max') ORDER BY date DESC LIMIT 5"
```

### Garmin Connect (unofficial, best-effort)

Garmin's official APIs are only open to approved business partners. fitness-hub uses the community `garminconnect`
library, which logs in the way the website does. **It can break without notice**: Garmin tightened its bot protection
in March 2026, and the library's API changes between versions.

1. Install with the `garmin` extra on Python 3.12 or newer (see Install).
2. Run `fitness-hub auth garmin` and enter your email, password and MFA code if asked. Only session tokens are saved,
   to `~/.fitness-hub/garmin-tokens/`. **Your password is never written to disk.**
3. Start with a short window, `fitness-hub sync --source garmin --since <7 days ago>`, then widen it. The daily stream
   makes several calls per day and pauses 0.4 s between days. If Garmin throttles it, the sync stops at that day and
   resumes there next time.

**If login stops working, use the CSV export.** In Garmin Connect on the web, go to **Activities**, choose
**Export CSV**, then run:

```bash
fitness-hub import-garmin-csv ~/Downloads/Activities.csv --distance-unit km --timezone Europe/Berlin
```

Use `--distance-unit mi` if your account shows miles. With that setting, ascent is treated as feet. Swims are
exported in metres (yards on imperial accounts), and the importer handles that. `--timezone` is
the zone the times in the CSV are in. Without it the times have no zone, and matching CSV activities against
Strava/Google is unreliable.

## Sync

```bash
fitness-hub sync                                  # all authorized sources, incremental
fitness-hub sync --source strava --source garmin  # some sources only
fitness-hub sync --since 2026-01-01               # reach back at least to this date
fitness-hub sync --full                           # ignore cursors and re-fetch the whole window
fitness-hub status                                # authorization, per-stream cursor/last success/last error, row counts
```

Each stream (Strava activities, Garmin daily, Google sleep, …) syncs and fails independently. Errors appear in
`status` and in the MCP `sync_status` tool. `sync` exits with code 1 if any stream failed. Re-running a sync never
creates duplicates. If you stop a sync partway, the database stays consistent and the next run picks up the gap.

To keep the data fresh, run the daemon:

```bash
fitness-hub run --interval-minutes 60            # sync every hour (survives errors; Ctrl-C to stop)
```

Or schedule `fitness-hub sync` with cron, launchd or systemd. Only one sync runs at a time: a second one exits with
"another sync is already running".

## Use it from Claude

### Claude Code

```bash
claude mcp add fitness-hub -- "$(pwd)/.venv/bin/fitness-hub" serve-mcp
```

### Claude Desktop

Add this to `claude_desktop_config.json`, using absolute paths:

```json
{
  "mcpServers": {
    "fitness-hub": {
      "command": "/absolute/path/to/performance-tracker-backend/.venv/bin/fitness-hub",
      "args": ["serve-mcp"],
      "env": { "FITNESS_HUB_HOME": "/Users/you/.fitness-hub" }
    }
  }
}
```

Then ask, for example:

- *"What was my weekly cycling volume for the last 8 weeks?"*
- *"What's my resting HR trend this month?"*
- *"How did my deep sleep compare on nights after long rides?"*
- *"Sync my data and tell me what changed this week."*

### Tools

| Tool | What it does |
|---|---|
| `sync_status` | Row counts and per-stream cursor, last success and last error |
| `list_activities` | Workouts newest first; filter by dates, sport (`cycling`, `running`, … or a provider type) and source; de-duplicated unless `source` is set |
| `get_activity` | One activity, optionally with the raw provider JSON |
| `activity_summary` | Totals per week (Monday start), month, day or sport: count, distance_km, moving_hours, elevation, calories (as recorded by the kept copy; the web API's `kcal` also falls back to a ride's kJ), average HR. Never double counts |
| `get_daily_metrics` | `{date, metric, source, value}` rows; default last 30 days. Sources are not merged |
| `get_sleep` | Sleep sessions by wake-up date; default last 30 days, main sleep only |
| `get_body_measurements` | Weight (kg) and body fat (%); default last 90 days |
| `query_sql` | One read-only `SELECT`/`WITH` query (at most 1000 rows, 1 MB per value, about 10 s; runs in a worker thread, so other tools stay responsive). Writes, `PRAGMA`, `ATTACH` and multiple statements are rejected |
| `training_load` | Daily TSS, fitness (CTL), fatigue (ATL) and form (TSB); default last 90 days |
| `power_curve` | Best average power per duration (1 s to 5 h) in a date range, with the activity that set it |
| `get_activity_analysis` | One activity's NP, IF, TSS, VI, EF, decoupling, zones, thresholds used and power curve |
| `nutrition_daily` | Per day: calories and nutrients eaten, workout kcal, the watch's total and active kcal, and the balance |
| `workout_fueling` | A workout's fueling: entries, totals and per-hour carbs, fluid, sodium and caffeine, against kcal burned |
| `sync_now` | Runs a sync and returns its log. It blocks, and `full=true` can hit rate limits |

Units: metres, seconds, bpm, watts, kcal, kg (summaries use km and hours). Timestamps are UTC, and dates are your local
date. The server's MCP instructions give Claude these details, plus the metric names and table schema.

### HTTP transport (optional)

```bash
fitness-hub serve-mcp --transport http --port 8800     # http://127.0.0.1:8800/mcp
fitness-hub run --http                                 # daemon + HTTP server in one process
claude mcp add --transport http fitness-hub http://127.0.0.1:8800/mcp
```

> **⚠ The HTTP transport has no authentication.** Any process on your machine can read your health data and trigger
> syncs through it. fitness-hub only binds to loopback (`127.0.0.1`, `localhost`, `::1`) and refuses any other host.
> Remote access with authentication is future work.

## Read API for the web UI

```bash
fitness-hub serve-api                  # http://127.0.0.1:8801/api, interactive docs at /docs
fitness-hub serve-api --host 0.0.0.0   # LAN mode: also serves other devices on your network
```

The [performance-tracker-ui](https://github.com/iamthanh/performance-tracker-ui) reads everything through this API.
It has **no authentication**. By default it serves only this machine:

- It binds to loopback (`API_HOST`, default `127.0.0.1`).
- It answers only requests whose `Host` is a loopback name, so a web page cannot reach it through DNS rebinding.
- Browsers may read responses only from the origins in `API_CORS_ORIGINS` (default: the UI's dev server,
  `http://localhost:5173` and `http://127.0.0.1:5173`).
- Writes with an `Origin` header must come from one of those or a loopback origin, and JSON bodies need
  `Content-Type: application/json`.

**LAN mode.** With `--host 0.0.0.0` (or `API_HOST=0.0.0.0` in `.env`) it also serves your local network, so the UI
works from a phone or another computer (see the UI's README). Anyone on the network can then read your data and change
thresholds, so use it only on a network you trust. In LAN mode:

- It answers only clients whose address is loopback, private (10/8, 172.16/12, 192.168/16, fc00::/7) or
  link-local (403 otherwise), even if the machine also has a public IPv4 or IPv6 address.
- `Host` (and the host of a write's `Origin`) may also be one of those IP addresses, or this machine's host name, bare
  or `.local`. Other names still get 421, so DNS rebinding stays blocked; add your own names, such as a router DNS
  entry, to `API_ALLOWED_HOSTS` (comma-separated).
- It refuses to bind to a public IP address. Prefer `0.0.0.0`: bound to one private address only, it no longer listens
  on 127.0.0.1, which is where the UI's proxy looks (set the UI's `FITNESS_HUB_API` to match).
- If a firewall is on, allow port 8801 from your network only (for example
  `sudo ufw allow from 192.168.1.0/24 to any port 8801 proto tcp`).

Reads use read-only connections. Writes are threshold overrides, which re-score the affected activities, and nutrition
(saved foods and logged entries).

| Endpoint | Returns |
|---|---|
| `GET /api/health` | Status, version, schema version, latest activity date, last analysis time |
| `GET /api/status` | Row counts and per-source sync state |
| `GET /api/activities?start_date&end_date&activity_type&limit` | Workouts newest first, one per workout (an analysed copy wins over an unanalysed duplicate), with TSS, IF, NP, work, VI, EF, decoupling, `kcal` burned (recorded calories from any copy of the workout, else a ride's kJ of work), `has_streams` |
| `POST /api/activities/import?filename&name&activity_type&timezone` | Import one `.fit`, `.gpx` or `.tcx` file (optionally `.gz`); the body is the file, as `application/octet-stream`. Stored as source `upload` and analysed; returns the workout and any copy already stored from another service |
| `GET /api/activities/{source}/{id}` | One workout: `activity`, `analysis` (zones, power curve, thresholds used), `laps`, `device`, available `streams` channels |
| `GET /api/activities/{source}/{id}/streams?max_points=2000` | Per-sample channels averaged down to `max_points` (`time` in seconds from the start) |
| `GET /api/summary?start_date&end_date&group_by=week` | Totals per day/week (Monday start)/month, overall and per sport: count, distance, hours, elevation, TSS, kJ, kcal burned |
| `GET /api/training-load?start_date&end_date` | Daily TSS, CTL, ATL, TSB and `unscored` workouts (default 90 days) |
| `GET /api/power-curve?start_date&end_date&activity_type` | Best power per duration with the activity that set it (cycling unless `activity_type=all`) |
| `GET /api/daily-metrics?metric=…`, `/api/sleep`, `/api/body` | Health data, as in the MCP tools |
| `GET /api/heart-rate/{date}` | All-day heart rate per source |
| `GET /api/thresholds?on=YYYY-MM-DD` | Every dated threshold value, and those in effect on `on` |
| `POST /api/thresholds` `{metric, effective_date, value, note?}` | Set a manual value; returns the re-scoring result |
| `DELETE /api/thresholds/{metric}/{effective_date}` | Remove a manual value |
| `GET /api/nutrition/foods?include_archived` | Saved foods, with nutrients per serving |
| `POST /api/nutrition/foods`, `PUT /api/nutrition/foods/{id}`, `DELETE …/{id}` | Save, replace or delete a food (logged entries keep their own copy) |
| `GET /api/nutrition/entries?start_date&end_date&activity_source&activity_source_id` | What was eaten and drunk, by dates and/or for one workout |
| `POST /api/nutrition/entries`, `PUT /api/nutrition/entries/{id}`, `DELETE …/{id}` | Log, replace or delete an entry |
| `GET /api/nutrition/daily?start_date&end_date` | Per day: intake totals, workout kcal, the watch's total and active kcal, balance |
| `GET /api/activities/{source}/{id}/fueling` | A workout's fueling: totals and per-hour rates against energy and (estimated) carbs burned; bottles' ingredients and glucose:fructose; weights and sweat rate |
| `PUT /api/activities/{source}/{id}/weight` `{pre_kg, post_kg, note?}` | Body weight before and after the workout (neither clears them) |

Errors return `{"detail": …}`:

- **422:** a malformed request (a missing or ill-typed parameter, or an invalid threshold body).
- **400:** a well-formed request with a bad date or range. Ranges are limited to 50 years, and the training load to a
  year ahead.
- **404:** an unknown activity.
- **503:** a database written by a newer fitness-hub.

## Manual verification checklist

The tests never contact the real services. To check against your own accounts:

1. Install, then run `pytest`; it should be green.
2. Strava: `fitness-hub auth strava`, then `fitness-hub sync --source strava --since <30 days ago>`, then `fitness-hub status`.
3. Google: complete the setup above, then `fitness-hub auth google` and `fitness-hub -v sync --source google_health`.
   Check the raw payloads for SpO2, respiratory rate, VO2 max and weight against the app.
4. Garmin: `fitness-hub auth garmin` and a 7-day sync. If login fails, use the CSV export.
5. `claude mcp add fitness-hub -- $(pwd)/.venv/bin/fitness-hub serve-mcp` and ask the questions above.
6. Check that a ride recorded by two services is counted once in `activity_summary`. Its `list_activities` entry
   should show `also_recorded_by`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `cannot listen on 127.0.0.1:8765` during auth | Something else is using the port. Free it, or set `OAUTH_PORT` and update the registered redirect URI |
| Strava `redirect_uri invalid` | The app's Authorization Callback Domain must be `localhost` |
| Google `redirect_uri_mismatch` | Register `http://localhost:8765/callback` exactly, on a **Web application** client |
| Google `invalid_grant` | The Testing-mode token expired (7 days): run `fitness-hub auth google` |
| Google 403 on one stream | That scope was not granted. Re-run auth and keep every box checked |
| Garmin `login` error | Run `fitness-hub auth garmin`. If it still fails, use `import-garmin-csv` |
| Nothing new from Google | The device has to sync to the phone app first |
| More detail | Add `-v`: `fitness-hub -v sync` (logs go to stderr) |

# How it works

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
├── __init__.py          # __version__
├── __main__.py          # python -m fitness_hub
├── cli.py               # fitness-hub command line
├── mcp_server.py        # MCP server (FastMCP): tools, instructions, transports
├── config.py            # Settings (env vars + .env files)
├── db.py                # schema, Store (idempotent upserts, sync_state), open_readonly()
├── http.py              # request_json: retry/backoff, RateLimited, ApiError
├── auth.py              # TokenStore (0600, atomic), PKCE, local OAuth redirect server
├── util.py              # timestamp/number parsing helpers
├── sync.py              # build_providers, run_sync, sync lock
├── queries.py           # read side: activities, summaries, metrics, sleep, body, run_select, de-dup
├── analysis.py          # training analysis: NP, TSS, zones, power curve, auto thresholds, load
├── nutrition.py         # foods, nutrition entries, daily energy in/out, workout fueling
├── api.py               # FastAPI read/write API for the web UI
├── activity_files.py    # FIT / GPX / TCX / Strava JSON parsing
└── providers/
    ├── __init__.py      # SyncWindow, SyncResult, run_stream
    ├── strava.py
    ├── google_health.py
    └── garmin.py        # provider + Garmin CSV importer
```

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
| `activity_streams` | `(source, source_id)` | `channels` (JSON list), `sample_count, encoding, data` (zlib-compressed JSON `{channel: [values]}`), `meta` |
| `activity_metrics` | `(source, source_id)` | Threshold-independent: `timer_s, avg_power_w, max_power_w, normalized_power_w, work_kj, variability_index, avg_hr, max_hr` (best 30 s)`, hr_20min_bpm, efficiency_factor, decoupling_pct, power_hist, hr_hist`. With the thresholds in effect that day: `ftp_w, threshold_hr, intensity_factor, tss, tss_method, variability_index, efficiency_factor, power_zone_s, hr_zone_s`. Plus `analysis_version, streams_updated_at, streams_bytes` |
| `daily_series` | `(source, date, series)` | `channels, sample_count, encoding, data`: intraday samples, `{"time": [epoch s], "heart_rate": [bpm]}` for series `heart_rate` |
| `power_curve` | `(source, source_id, duration_s)` | `watts`: the best average power over `duration_s` in that activity |
| `athlete_settings` | `(metric, effective_date, origin)` | `value, note`. Metrics `ftp_w, threshold_hr, max_hr, resting_hr`; origin `manual` or `auto` (see Training analysis for how they combine) |
| `foods` | `id` | `name, brand, serving` and nutrients **per serving**: `kcal, carbs_g, sugars_g, protein_g, fat_g, fiber_g, sodium_mg, potassium_mg, caffeine_mg, fluid_ml`; `note, archived` |
| `nutrition_entries` | `id` (and `(source, source_id)` for imports) | `local_date, consumed_at, meal` (`breakfast, lunch, dinner, snack, pre_workout, during_workout, post_workout`), `activity_source, activity_source_id, offset_s` (the workout it fuelled, seconds into it), `food_id, name, servings` (bottles, for a drink), the same nutrients as **totals for the entry**, `note`, `kind` (`food` or `drink`), `recipe` (one bottle, JSON) |
| `workout_weights` | `(source, source_id)` | `pre_kg, post_kg, note`: body weight before and after the workout |

**Schema versions.** The version is kept in `PRAGMA user_version`. Opening the database applies any missing migrations
(`db.MIGRATIONS`), each in one transaction. A database created before versioning counts as version 1, and a database
newer than the installed fitness-hub is refused rather than misread.

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

**Cursors:** Strava `activities` stores the newest `start_time` seen. Day-based streams store the window end. When
Garmin `daily` is throttled partway, it stores the last fully synced day as a partial cursor (`YYYY-MM-DD/partial`).
The next run resumes on the following day, without the lookback overlap, so a throttled backfill always moves forward.
Once a run completes, the cursor is a normal date again.

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
- Uses `GET /athlete/activities?after=…&per_page=200&page=N` until an **empty** page arrives (Strava can return
  short pages before the end). Summary activities have
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
  - `activities`: `get_activities_by_date(start, end)`, only with `GARMIN_ACTIVITIES=true`.
  - `daily`: for each day, `get_stats` (steps, resting HR, stress, body battery, calories, distance, floors, SpO2,
    respiration), `get_sleep_data` (a session with stages plus `sleep_score`), `get_hrv_data` (`hrv_rmssd_ms`) and
    `get_heart_rates` (all-day heart rate, about one sample every two minutes, stored in `daily_series`). Many devices
    have no HRV, so HRV and all-day HR errors only skip that part of the day. It pauses 0.4 s between days.
    - A day with no data is skipped, so the stream doesn't stall on days before you owned the watch.
      garminconnect reports such days as "No data received" or a privacy-protected summary.
    - Throttling, authentication errors and server errors stop the stream at that day.
  - `body`: `get_body_composition(start, end)`; weights arrive in grams and are converted to kg. Times come from
    `timestampGMT`, because `date` is local time encoded as epoch ms.
- **CSV fallback:** in Garmin Connect on the web, go to Activities and choose **Export CSV**, then import the file with
  `import_garmin_csv(store, path, distance_unit="km"|"mi", timezone=None)`. It expects English column names, handles
  thousands separators and `h:mm:ss` times, reads swim distances as metres or yards, and converts miles and feet
  when `distance_unit` is `mi`. Without a
  timezone, start times are stored without a zone.

## Cross-service de-duplication (at read time)

The same workout often shows up two or three times (watch → Garmin → Strava, and the same watch through Google Health).
`queries.dedupe_activities` keeps one activity per workout, preferring **garmin > google_health > strava >
garmin_export**, and lists the other sources in `also_recorded_by`. An activity from a **different** source is treated
as a duplicate when it starts within **180 s** of the activity that is kept. Groups are seeded in priority order, so
they cannot chain into one long group. Source rows are never deleted. `list_activities` de-duplicates unless a `source` is given,
and `activity_summary` always does, so totals never count a workout twice. Daily metrics are **not** merged, because the
same metric can legitimately differ between sources.

`sport_family()` maps each provider's activity type to a common family (cycling, running, swimming, walking, hiking,
rowing, strength, skiing, yoga). That way an `activity_type="cycling"` filter matches Strava `Ride`, Garmin
`road_biking` and Google `OUTDOOR_BIKE`.

## Read-only SQL

`queries.run_select` accepts exactly one `SELECT`/`WITH` statement; SQLite's own parser rejects a second statement,
so a `;` inside a string literal is fine. As further protection, it installs a SQLite authorizer that only allows reads.
That means `WITH … DELETE`, `PRAGMA`, `ATTACH`, `load_extension` and `fts3_tokenizer` are refused even on a read-write
connection.

Runtime is bounded too. Every string or blob value is capped at 1 MB, and LIKE patterns at 1000 characters. A
10-second time limit is checked every 1000 SQLite steps. The limit is best effort: one function call cannot be
interrupted, but the size cap keeps the slowest case, `instr()` on two maximal strings, to about 6 s.

The MCP server runs these queries in a worker thread, on a `mode=ro` + `PRAGMA query_only=ON` connection, so a slow
query never blocks the other tools.

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
8. The **HTTP transport has no authentication** and is localhost-only.
9. Single user, on your machine or home network. Nothing is written back to the providers, and no GPS or FIT streams are stored.

## Future work

- **Authentication for the read API**, so the UI can be used from other devices (today it is localhost-only).

From the plan, in priority order:

1. Lock the Google mappings from real payloads, then add `heart-rate` and `daily-sleep-temperature-derivations`.
2. Strava detail fetch (calories, splits, streams) with a queue that respects rate limits, and Strava webhooks.
3. Google Takeout importer and Garmin bulk-export JSON importer.
4. Scheduling recipes: launchd, systemd timer, Dockerfile.
5. Remote MCP with authentication.
6. Token encryption at rest (OS keychain).
7. Derived training-load, sleep-regularity and HRV-baseline views.
8. Webhook-driven Google Health sync.
