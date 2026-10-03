"""The programs that sign in with a password (clara-chat, clara-admin): the token is kept, and renewed when refused."""

import socket
from types import SimpleNamespace

import httpx
import pytest

from clara import session

PASSWORD = "correct horse battery"


@pytest.fixture
def server(live, monkeypatch, tmp_path):
    app, url = live
    app.state.users.create("erwan", PASSWORD)
    app.state.users.create("root", PASSWORD, admin=True)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("CLARA_PASSWORD", raising=False)
    return app, url


def test_login_gives_a_token_the_server_accepts(server):
    _, url = server
    token = session.login(url, "erwan", PASSWORD, "cli")
    assert token.startswith("clu_") and session.still_valid(url, token)
    reply = httpx.get(f"{url}/v1/memory/facts", params={"surface": "cli", "user_id": "erwan"},
                      headers={"Authorization": f"Bearer {token}"})
    assert reply.status_code in (200, 404)


def test_a_wrong_password_or_unreachable_server_is_a_login_error(server):
    _, url = server
    with pytest.raises(session.LoginError, match="Wrong user name or password"):
        session.login(url, "erwan", "not the password", "cli")
    with socket.socket() as sock:  # a port nobody listens on
        sock.bind(("127.0.0.1", 0))
        closed = f"http://127.0.0.1:{sock.getsockname()[1]}"
    with pytest.raises(session.LoginError, match="Cannot reach"):
        session.login(closed, "erwan", PASSWORD, "cli", timeout=5)


def test_the_token_is_cached_and_the_password_asked_only_once(server, monkeypatch):
    _, url = server
    monkeypatch.setenv("CLARA_PASSWORD", PASSWORD)
    first = session.obtain_token(url, "erwan", "cli")
    monkeypatch.setenv("CLARA_PASSWORD", "no longer needed at all")
    assert session.obtain_token(url, "erwan", "cli") == first
    assert session.cached(url, "erwan", "cli") == first
    assert PASSWORD not in session.cache_file().read_text(encoding="utf-8")  # only the token is kept


def test_a_revoked_token_is_replaced_by_signing_in_again(server, monkeypatch):
    app, url = server
    monkeypatch.setenv("CLARA_PASSWORD", PASSWORD)
    first = session.obtain_token(url, "erwan", "cli")
    app.state.users.revoke_all("erwan")
    assert not session.still_valid(url, first)
    second = session.obtain_token(url, "erwan", "cli")
    assert second != first and session.still_valid(url, second)


def test_without_a_terminal_and_without_a_password_it_says_what_to_do(server, monkeypatch):
    _, url = server
    monkeypatch.setattr(session, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: False)))
    with pytest.raises(session.LoginError, match="CLARA_PASSWORD"):
        session.obtain_token(url, "erwan", "cli")


def test_signing_out_revokes_the_token_on_the_server(server, monkeypatch):
    _, url = server
    monkeypatch.setenv("CLARA_PASSWORD", PASSWORD)
    token = session.obtain_token(url, "erwan", "cli")
    assert session.sign_out(url, "erwan", "cli") and not session.sign_out(url, "erwan", "cli")
    assert session.cached(url, "erwan", "cli") is None and not session.still_valid(url, token)


def test_an_administrator_can_open_the_remote_console_with_a_password(server, monkeypatch):
    _, url = server
    monkeypatch.setenv("CLARA_PASSWORD", PASSWORD)
    token = session.obtain_token(url, "root", "console")
    reply = httpx.post(f"{url}/v1/admin/command", json={"line": "/status"}, headers={"Authorization": f"Bearer {token}"})
    assert reply.status_code == 200 and "Provider" in reply.json()["output"]
    plain = session.obtain_token(url, "erwan", "console")
    denied = httpx.post(f"{url}/v1/admin/command", json={"line": "/status"}, headers={"Authorization": f"Bearer {plain}"})
    assert denied.status_code == 403
