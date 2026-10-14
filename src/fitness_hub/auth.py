"""Token storage and the local OAuth redirect helper.

Tokens live in ``$FITNESS_HUB_HOME/tokens.json`` (mode 0600, written
atomically). They are never stored in the database and never logged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """Authentication problem the user must fix (message says how)."""


class TokenStore:
    """JSON file of per-provider token dicts, owner-read/write only."""

    _lock = threading.Lock()

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load_all(self) -> dict[str, dict[str, Any]]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AuthError(f"{self.path} is corrupt ({exc}); delete it and re-run auth") from exc
        return data if isinstance(data, dict) else {}

    def get(self, provider: str) -> dict[str, Any] | None:
        value = self.load_all().get(provider)
        return dict(value) if isinstance(value, dict) else None

    def save(self, provider: str, data: dict[str, Any]) -> None:
        with self._lock:
            all_tokens = self.load_all()
            all_tokens[provider] = data
            self._write(all_tokens)

    def delete(self, provider: str) -> None:
        with self._lock:
            all_tokens = self.load_all()
            if all_tokens.pop(provider, None) is not None:
                self._write(all_tokens)

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tokens.", dir=self.path.parent)  # created 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise


def expires_soon(token: dict[str, Any], margin: float = 60.0, now: float | None = None) -> bool:
    """True when the access token is missing or expires within ``margin`` seconds."""
    if not token.get("access_token"):
        return True
    try:
        expires_at = float(token.get("expires_at") or 0)
    except (TypeError, ValueError):
        return True
    return expires_at < (time.time() if now is None else now) + margin


def make_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, S256 code_challenge)``."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def new_state() -> str:
    return secrets.token_urlsafe(24)


_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>fitness-hub</title></head>
<body style="font-family:sans-serif;margin:3em"><h2>{title}</h2><p>{body}</p></body></html>"""


def wait_for_callback(
    port: int,
    expected_state: str,
    timeout: float = 300.0,
    host: str = "127.0.0.1",
) -> dict[str, str]:
    """Serve ``/callback`` on ``host:port`` until a request with the expected
    ``state`` arrives; return its query parameters. Raises :class:`AuthError`
    on provider error, or on timeout."""
    result: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self._reply(404, "Not found", "This server only handles the OAuth callback.")
                return
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            if not secrets.compare_digest(params.get("state", ""), expected_state):
                self._reply(400, "State mismatch", "Ignoring this request; restart auth.")
                return
            result["params"] = params
            if "error" in params:
                self._reply(400, "Authorization failed", "You can close this tab.")
            else:
                self._reply(200, "fitness-hub is authorized", "You can close this tab.")

        def _reply(self, code: int, title: str, body: str) -> None:
            payload = _PAGE.format(title=title, body=body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return  # the query string contains the auth code; keep it out of logs

    try:
        server = HTTPServer((host, port), Handler)
    except OSError as exc:
        raise AuthError(
            f"cannot listen on {host}:{port} for the OAuth redirect ({exc}); "
            "free the port or set OAUTH_PORT (and update the registered redirect URI)"
        ) from exc
    server.timeout = 0.5
    deadline = time.monotonic() + timeout
    try:
        while "params" not in result:
            if time.monotonic() > deadline:
                raise AuthError(f"timed out after {timeout:.0f}s waiting for the OAuth redirect")
            server.handle_request()
    finally:
        server.server_close()
    params: dict[str, str] = result["params"]
    if "error" in params:
        detail = params.get("error_description") or params["error"]
        raise AuthError(f"authorization was denied or failed: {detail}")
    if not params.get("code"):
        raise AuthError("OAuth redirect did not include an authorization code")
    return params


def run_browser_flow(
    authorize_url: str,
    port: int,
    state: str,
    *,
    open_browser: bool = True,
    timeout: float = 300.0,
    echo: Callable[[str], None] = print,
) -> dict[str, str]:
    """Print/open the consent URL and wait for the redirect."""
    echo("Open this URL in a browser to authorize fitness-hub:\n\n  " + authorize_url + "\n")
    if open_browser:
        try:
            webbrowser.open(authorize_url)
        except Exception:  # noqa: BLE001 - headless machines have no browser
            pass
    echo(f"Waiting for the redirect on http://localhost:{port}/callback (timeout {timeout:.0f}s)...")
    return wait_for_callback(port, state, timeout=timeout)
