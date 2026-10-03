"""API 接口测试：启动真实 HTTP 服务，走完整请求-响应链路。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from campusnet import server as server_mod
from campusnet.monitor import MonitorManager
from campusnet.storage import Account, Store


@pytest.fixture()
def api(store: Store):  # noqa: ANN001
    manager = MonitorManager()
    handler = server_mod.build_handler(store, manager)
    server_mod._install_dispatch(handler)
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(url: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:  # noqa: ANN001
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def test_health(api: str) -> None:
    status, data = call(f"{api}/api/health")
    assert status == 200 and data["ok"] is True
    assert data["accounts"] == 0


def test_healthz(api: str) -> None:
    status, data = call(f"{api}/healthz")
    assert status == 200 and data["ok"] is True


def test_account_crud_flow(api: str) -> None:
    status, created = call(
        f"{api}/api/accounts",
        "POST",
        {
            "name": "宿舍",
            "portal_url": "http://10.0.0.1/eportal/portal/login",
            "username": "20240001",
            "password": "p@ss",
            "login_mode": "http",
            "password_mode": "base64",
            "max_retries": 2,
        },
    )
    assert status == 201, created
    acc_id = created["id"]
    assert "password" not in created["account"]

    status, listed = call(f"{api}/api/accounts")
    assert status == 200 and len(listed["accounts"]) == 1

    status, updated = call(
        f"{api}/api/accounts/{acc_id}",
        "PUT",
        {"name": "宿舍2", "portal_url": "http://10.0.0.1/x", "username": "u2"},
    )
    assert status == 200 and updated["account"]["name"] == "宿舍2"

    status, _ = call(f"{api}/api/accounts/{acc_id}", "DELETE")
    assert status == 200

    status, data = call(f"{api}/api/accounts/{acc_id}", "DELETE")
    assert status == 404 and data["ok"] is False


def test_update_missing_account_404(api: str) -> None:
    status, data = call(f"{api}/api/accounts/999", "PUT", {"name": "x", "portal_url": "http://a"})
    assert status == 404
    assert "不存在" in data["error"]


def test_create_validation_errors(api: str) -> None:
    status, data = call(f"{api}/api/accounts", "POST", {"portal_url": "http://a"})
    assert status == 400 and "名称" in data["error"]

    status, data = call(f"{api}/api/accounts", "POST", {"name": "a"})
    assert status == 400 and "门户地址" in data["error"]

    status, data = call(f"{api}/api/accounts", "POST", {"name": "a", "portal_url": "ftp://a"})
    assert status == 400 and "http" in data["error"]

    status, data = call(
        f"{api}/api/accounts",
        "POST",
        {"name": "a", "portal_url": "http://a", "login_mode": "telepathy"},
    )
    assert status == 400 and "登录模式" in data["error"]

    status, data = call(
        f"{api}/api/accounts",
        "POST",
        {"name": "a", "portal_url": "http://a", "password_mode": "rot13"},
    )
    assert status == 400 and "密码编码" in data["error"]


def test_invalid_json_body(api: str) -> None:
    req = urllib.request.Request(
        f"{api}/api/accounts", data=b"{not json", method="POST"
    )
    req.add_header("Content-Type", "application/json")
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError("应当返回 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
        assert "JSON" in exc.read().decode("utf-8")


def test_method_not_allowed(api: str) -> None:
    status, data = call(f"{api}/api/health", "POST", {})
    assert status == 405
    assert "GET" in data["error"]


def test_unknown_api_path_returns_json_404(api: str) -> None:
    status, data = call(f"{api}/api/nope")
    assert status == 404 and data["ok"] is False


def test_probe_endpoint(api: str) -> None:
    status, data = call(f"{api}/api/probe?url=http://127.0.0.1:9/x&timeout=2")
    assert status == 200
    assert "result" in data and isinstance(data["result"]["online"], bool)


def test_detect_portal_endpoint(api: str) -> None:
    status, data = call(f"{api}/api/detect-portal?url=http://127.0.0.1:9/x")
    assert status == 200
    assert "portal_url" in data


def test_discover_endpoint_without_scan(api: str) -> None:
    status, data = call(f"{api}/api/discover?seed=http://127.0.0.1:9/x&timeout=1&scan=0")
    assert status == 200
    assert data["ok"] is True
    assert data["report"]["ok"] is False
    assert data["report"]["probe_candidates"]
    assert data["best"] is None
    assert data["account_fields"] is None


def test_discover_endpoint_fills_account_fields(api: str, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """探到门户时要把可直接建号的字段一并返回。"""
    from campusnet import discovery as discovery_mod

    candidate = discovery_mod.PortalCandidate(
        url="http://10.0.0.1/eportal/portal/login",
        status=200,
        title="Dr.COM",
        vendor="Dr.COM / 深澜",
        username_field="DDDDD",
        password_field="upass",
        extra_fields={"0MKKey": ""},
        suggested_login_mode="http",
        suggested_password_mode="base64",
    )
    monkeypatch.setattr(
        server_mod.discovery,
        "discover",
        lambda **_: {
            "ok": True,
            "candidates": [candidate.as_dict()],
            "hints": ["最佳候选"],
        },
    )

    status, data = call(f"{api}/api/discover?seed=http://x/&scan=0")
    assert status == 200
    assert data["best"]["vendor"] == "Dr.COM / 深澜"
    assert data["account_fields"]["username_field"] == "DDDDD"
    assert data["account_fields"]["password_mode"] == "base64"
    assert data["account_fields"]["extra_fields"] == {"0MKKey": ""}


def test_login_endpoint_reports_missing_account(api: str) -> None:
    status, data = call(f"{api}/api/accounts/42/login", "POST", {})
    assert status == 404 and "不存在" in data["error"]


def test_login_endpoint_runs_manual_mode(api: str, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    from campusnet.portal import LoginResult

    monkeypatch.setattr(
        server_mod.portal, "login", lambda *a, **k: LoginResult("success", "已放行", "detail")
    )
    _, created = call(
        f"{api}/api/accounts",
        "POST",
        {"name": "手动", "portal_url": "http://10.0.0.1/x", "username": "u", "password": "p"},
    )
    status, data = call(f"{api}/api/accounts/{created['id']}/login", "POST", {})
    assert status == 200
    assert data["state"] == "success" and data["message"] == "已放行"


def test_monitor_start_stop_and_status(api: str) -> None:
    status, data = call(f"{api}/api/monitor/start", "POST", {"interval": 300})
    assert status == 200 and data["status"]["interval"] == 300
    assert data["status"]["alive"] is True

    status, data = call(f"{api}/api/monitor")
    assert status == 200 and data["status"]["alive"] is True

    status, data = call(f"{api}/api/monitor/stop", "POST", {})
    assert status == 200 and data["stopped"] is True

    status, data = call(f"{api}/api/monitor")
    assert status == 200 and data["status"]["alive"] is False


def test_monitor_start_is_idempotent(api: str) -> None:
    _, first = call(f"{api}/api/monitor/start", "POST", {})
    _, second = call(f"{api}/api/monitor/start", "POST", {})
    assert first["started"] is True
    assert second["started"] is False
    call(f"{api}/api/monitor/stop", "POST", {})


def test_monitor_check_endpoint(api: str) -> None:
    status, data = call(f"{api}/api/check", "POST", {})
    assert status == 200
    assert data["results"] == []


def test_events_endpoint(api: str) -> None:
    _, created = call(
        f"{api}/api/accounts",
        "POST",
        {"name": "日志", "portal_url": "http://10.0.0.1/x", "username": "u", "password": "p"},
    )
    status, data = call(f"{api}/api/events?limit=10")
    assert status == 200
    assert any("新增账号" in e["message"] for e in data["events"])

    status, scoped = call(f"{api}/api/events?account_id={created['id']}")
    assert status == 200 and scoped["events"]

    status, _ = call(f"{api}/api/events/clear", "POST", {})
    assert status == 200
    _, after = call(f"{api}/api/events")
    assert after["events"] == []


def test_settings_get_and_put(api: str) -> None:
    status, data = call(f"{api}/api/settings", "PUT", {"interval": 90, "headless": False})
    assert status == 200 and data["settings"]["interval"] == 90

    status, data = call(f"{api}/api/settings")
    assert status == 200 and data["settings"]["headless"] is False


def test_static_index_is_served(api: str) -> None:
    with urllib.request.urlopen(f"{api}/", timeout=10) as resp:
        html = resp.read().decode("utf-8")
    assert resp.status == 200
    assert "校园网自连" in html


def test_static_missing_file_404(api: str) -> None:
    status, data = call(f"{api}/nope.html")
    assert status == 404 and data["ok"] is False


def test_path_traversal_blocked(api: str) -> None:
    status, _ = call(f"{api}/../config.json")
    assert status in (403, 404)


def test_server_exception_returns_500(api: str, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        server_mod.Store, "list_accounts", lambda self: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    status, data = call(f"{api}/api/accounts")
    assert status == 500
    assert "boom" in data["error"]


def test_account_password_kept_when_blank_on_update(api: str) -> None:
    _, created = call(
        f"{api}/api/accounts",
        "POST",
        {"name": "保留", "portal_url": "http://10.0.0.1/x", "username": "u", "password": "orig"},
    )
    status, _ = call(
        f"{api}/api/accounts/{created['id']}",
        "PUT",
        {"name": "改名", "portal_url": "http://10.0.0.1/x", "username": "u", "password": ""},
    )
    assert status == 200

    status, data = call(f"{api}/api/accounts")
    assert data["accounts"][0]["name"] == "改名"
