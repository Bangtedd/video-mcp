"""Password login with a signed, HTTP-only session cookie that also carries the
CSRF token. No server-side session state, so restarting the app keeps people
logged in; changing WEB_PASSWORD logs everyone out."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

COOKIE = "vmw_session"
LOGIN_COOKIE = "vmw_login"     # pre-login CSRF token (double submit)
MAX_AGE = 30 * 24 * 3600


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def load_secret(workspace_root: Path) -> bytes:
    """A random secret kept in the workspace (mode 0600), created on first start."""
    path = workspace_root / ".web_secret"
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    except FileNotFoundError:
        pass
    data = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return data


class Signer:
    def __init__(self, secret: bytes, password: str):
        # Bind the key to the password: changing it invalidates existing cookies.
        self.key = hmac.new(secret, password.encode(), hashlib.sha256).digest()
        self.password = password

    def check_password(self, supplied: str) -> bool:
        return hmac.compare_digest(
            hashlib.sha256(supplied.encode()).digest(),
            hashlib.sha256(self.password.encode()).digest(),
        )

    def _sig(self, payload: bytes) -> str:
        return _b64(hmac.new(self.key, payload, hashlib.sha256).digest())

    def make_session(self) -> tuple[str, str]:
        """(cookie value, csrf token) for a freshly logged-in browser."""
        csrf = secrets.token_hex(16)
        payload = json.dumps(
            {"exp": int(time.time()) + MAX_AGE, "csrf": csrf}, separators=(",", ":")
        ).encode()
        return f"{_b64(payload)}.{self._sig(payload)}", csrf

    def read_session(self, value: str | None) -> dict | None:
        if not value or "." not in value:
            return None
        body, sig = value.rsplit(".", 1)
        try:
            payload = _unb64(body)
        except ValueError:
            return None
        if not hmac.compare_digest(sig, self._sig(payload)):
            return None
        try:
            data = json.loads(payload)
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("exp", 0) < time.time():
            return None
        if not isinstance(data.get("csrf"), str):
            return None
        return data
