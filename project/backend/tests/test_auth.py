"""局域网控制台认证链路测试。"""

from __future__ import annotations

import json
import os
import stat
import threading
import urllib.error
import urllib.request

from http.server import ThreadingHTTPServer

import pytest

from campusnet import auth as auth_mod
from campusnet import server as server_mod
from campusnet.auth import ApiAuth
from campusnet.monitor import MonitorManager
from campusnet.storage import Store


def _request(
    base: str,
    path: str,
    method: str = "GET",
    body: dict | None = None,
    cookie: str = "",
) -> tuple[int, dict, dict[str, str]]:
    raw = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(base + path, data=raw, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if cookie:
        request.add_header("Cookie", cookie)
    def _decode(raw: bytes) -> dict:
        text = raw.decode("utf-8")
        return json.loads(text) if text.strip() else {}

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, _decode(response.read()), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read()), dict(exc.headers)


def test_auth_token_file_and_session(tmp_path) -> None:  # noqa: ANN001
    auth = ApiAuth(tmp_path / "token", token="a" * 32)
    assert auth.check_token("a" * 32)
    assert not auth.check_token("b" * 32)
    session = auth.login("a" * 32)
    assert session
    cookie = {"Cookie": ApiAuth.set_cookie(session)}
    assert auth.authenticated(cookie)
    auth.logout(cookie)
    assert not auth.authenticated(cookie)


def test_token_is_created_and_reused(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "private" / "console.api-token"
    first = ApiAuth(path)
    assert path.read_text(encoding="utf-8").strip() == first.token
    assert len(first.token) >= 32
    assert ApiAuth(path).token == first.token
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not Windows ACLs")
def test_existing_token_permissions_are_restricted(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "console.api-token"
    path.write_text("a" * 32 + "\n", encoding="utf-8")
    path.chmod(0o644)
    auth = ApiAuth(path)
    assert auth.token == "a" * 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not Windows ACLs")
def test_existing_token_rejects_failed_permission_restriction(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    path = tmp_path / "console.api-token"
    path.write_text("a" * 32, encoding="utf-8")
    path.chmod(0o644)
    monkeypatch.setattr(path.__class__, "chmod", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("denied")))
    with pytest.raises(RuntimeError, match="权限"):
        ApiAuth(path)


def test_authentication_headers_and_expiring_session(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    auth = ApiAuth(tmp_path / "token", token="a" * 32)
    assert auth.authenticated({"Authorization": "Bearer " + "a" * 32})
    assert auth.authenticated({"Authorization": "bearer " + "a" * 32})
    assert auth.authenticated({"X-CampusNet-Token": "a" * 32})
    assert not auth.authenticated({"Authorization": "Bearer invalid"})
    assert not auth.authenticated({"X-CampusNet-Token": "invalid"})
    with monkeypatch.context() as patch:
        patch.setattr(auth_mod.time, "time", lambda: 1000.0)
        session = auth.login("a" * 32)
        assert session is not None
        cookie = {"Cookie": f"campusnet_session={session}"}
        assert auth.authenticated(cookie)
        patch.setattr(auth_mod.time, "time", lambda: 1000.0 + auth_mod.SESSION_TTL)
        assert not auth.authenticated(cookie)
        assert session not in auth._sessions


def test_malformed_cookie_does_not_authorize(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    auth = ApiAuth(tmp_path / "token", token="a" * 32)
    assert not auth.authenticated({"Cookie": "campusnet_session=\"unterminated"})

    class BadCookie:
        def load(self, _raw):  # noqa: ANN001
            raise auth_mod.cookies.CookieError("malformed")

    monkeypatch.setattr(auth_mod.cookies, "SimpleCookie", BadCookie)
    assert not auth.authenticated({"Cookie": "invalid"})


def test_auth_protects_business_api_and_keeps_health_public(tmp_path) -> None:  # noqa: ANN001
    store = Store(tmp_path / "auth.db")
    auth = ApiAuth(tmp_path / "token", token="a" * 32)
    manager = MonitorManager()
    handler = server_mod.build_handler(store, manager, auth=auth, auth_required=True)
    server_mod._install_dispatch(handler)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, health, _ = _request(base, "/healthz")
        assert status == 200 and health["ok"] is True

        status, denied, _ = _request(base, "/api/accounts")
        assert status == 401 and denied["ok"] is False

        status, denied, _ = _request(base, "/api/auth/login", "POST", {"token": "wrong"})
        assert status == 401 and denied["ok"] is False

        status, logged, headers = _request(base, "/api/auth/login", "POST", {"token": "a" * 32})
        assert status == 200 and logged["authenticated"] is True
        cookie = headers["Set-Cookie"].split(";", 1)[0]

        status, accounts, _ = _request(base, "/api/accounts", cookie=cookie)
        assert status == 200 and accounts["accounts"] == []

        status, _, _ = _request(base, "/api/auth/logout", "POST", {}, cookie=cookie)
        assert status == 200
        status, _, _ = _request(base, "/api/accounts", cookie=cookie)
        assert status == 401

        status, _, headers = _request(base, "/api/accounts", method="OPTIONS")
        assert status == 204
        assert "OPTIONS" in headers["Allow"]
    finally:
        httpd.shutdown()
        httpd.server_close()
