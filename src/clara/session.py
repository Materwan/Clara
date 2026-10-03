"""For the programs that talk to the server (`clara-chat`, `clara-admin`): sign in with a password once, then keep
the token the server hands out. The password is never stored; the token is, in a file only the user can read,
until the server revokes it (sign out, a new password, 90 days unused).
"""

from __future__ import annotations

import getpass
import json
import os
import socket
import stat
import sys
from pathlib import Path

import httpx


class LoginError(Exception):
    """Could not sign in; the text says why."""


def cache_file() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "clara" / "sessions.json"


def _key(url: str, user: str, surface: str) -> str:
    return f"{url.rstrip('/')}|{user}|{surface}"


def _read() -> dict[str, str]:
    try:
        data = json.loads(cache_file().read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    except (OSError, ValueError, AttributeError):
        return {}


def _write(data: dict[str, str]) -> None:
    path = cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # a no-op on Windows, where the profile folder is private
    except OSError:
        pass


def cached(url: str, user: str, surface: str) -> str | None:
    return _read().get(_key(url, user, surface))


def remember(url: str, user: str, surface: str, token: str) -> None:
    data = _read()
    data[_key(url, user, surface)] = token
    _write(data)


def forget(url: str, user: str, surface: str) -> str | None:
    data = _read()
    token = data.pop(_key(url, user, surface), None)
    if token is not None:
        _write(data)
    return token


def login(url: str, user: str, password: str, surface: str, timeout: float = 15.0) -> str:
    """A fresh token for `user`, or LoginError."""
    try:
        response = httpx.post(
            url.rstrip("/") + "/v1/auth/login",
            json={"username": user, "password": password, "surface": surface, "device": socket.gethostname()},
            timeout=timeout,
        )
    except httpx.HTTPError as error:
        raise LoginError(f"Cannot reach the Clara server at {url} ({type(error).__name__}).") from None
    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise LoginError(str(detail))
    return response.json()["token"]


def still_valid(url: str, token: str, timeout: float = 15.0) -> bool:
    """False only when the server says the token is no longer good (an unreachable server is not that)."""
    try:
        response = httpx.get(
            url.rstrip("/") + "/v1/auth/me", headers={"Authorization": f"Bearer {token}"}, timeout=timeout
        )
    except httpx.HTTPError:
        return True
    return response.status_code != 401


def sign_out(url: str, user: str, surface: str) -> bool:
    """Forget the cached token and tell the server to revoke it."""
    token = forget(url, user, surface)
    if token is None:
        return False
    try:
        httpx.post(url.rstrip("/") + "/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}, timeout=10.0)
    except httpx.HTTPError:
        pass
    return True


def obtain_token(url: str, user: str, surface: str, password: str | None = None) -> str:
    """The cached token if the server still accepts it; else sign in with `password` (or ask for it)."""
    token = cached(url, user, surface)
    if token and still_valid(url, token):
        return token
    password = password or os.environ.get("CLARA_PASSWORD")
    if not password:
        if not sys.stdin.isatty():
            raise LoginError("Not signed in: set CLARA_PASSWORD, or run this program once in a terminal.")
        password = getpass.getpass(f"Password for {user} on {url}: ")
    token = login(url, user, password, surface)
    remember(url, user, surface, token)
    return token
