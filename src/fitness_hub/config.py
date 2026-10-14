"""Settings loaded from environment variables and optional ``.env`` files.

Precedence (highest first): the real process environment, then
``$FITNESS_HUB_HOME/.env``, then ``./.env``. ``os.environ`` is never modified.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOME = "~/.fitness-hub"


class ConfigError(ValueError):
    """Raised for invalid configuration values."""


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse a minimal ``.env`` file: ``KEY=VALUE`` lines, ``#`` comments,
    optional ``export`` prefix and optional single/double quotes."""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if value[:1] in ("'", '"') and value.find(value[0], 1) != -1:
            value = value[1 : value.find(value[0], 1)]  # anything after the closing quote is ignored
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def _read_dotenv(path: Path) -> dict[str, str]:
    try:
        return parse_dotenv(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError as exc:  # unreadable .env should not be silently ignored
        raise ConfigError(f"cannot read {path}: {exc}") from exc


def _int(env: Mapping[str, str], key: str, default: int, minimum: int = 0) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    home: Path
    strava_client_id: str | None = None
    strava_client_secret: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    garmin_email: str | None = None
    garmin_password: str | None = None
    oauth_port: int = 8765
    backfill_days: int = 365
    lookback_days: int = 7

    @property
    def db_path(self) -> Path:
        return self.home / "fitness.db"

    @property
    def tokens_path(self) -> Path:
        return self.home / "tokens.json"

    @property
    def garmin_token_dir(self) -> Path:
        return self.home / "garmin-tokens"

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.oauth_port}/callback"

    def ensure_home(self) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        return self.home

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, cwd: Path | None = None
    ) -> Settings:
        env = dict(os.environ if environ is None else environ)
        local = _read_dotenv((cwd or Path.cwd()) / ".env")
        home_raw = env.get("FITNESS_HUB_HOME") or local.get("FITNESS_HUB_HOME") or DEFAULT_HOME
        home = Path(home_raw).expanduser()
        merged = {**local, **_read_dotenv(home / ".env"), **env}

        def opt(key: str) -> str | None:
            value = merged.get(key)
            return value if value else None

        return cls(
            home=home,
            strava_client_id=opt("STRAVA_CLIENT_ID"),
            strava_client_secret=opt("STRAVA_CLIENT_SECRET"),
            google_client_id=opt("GOOGLE_CLIENT_ID"),
            google_client_secret=opt("GOOGLE_CLIENT_SECRET"),
            garmin_email=opt("GARMIN_EMAIL"),
            garmin_password=opt("GARMIN_PASSWORD"),
            oauth_port=_int(merged, "OAUTH_PORT", 8765, minimum=1),
            backfill_days=_int(merged, "BACKFILL_DAYS", 365),
            lookback_days=_int(merged, "LOOKBACK_DAYS", 7),
        )
