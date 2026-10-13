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


class _Api(str):
    """既是 base URL 字符串，又带着 server 实际在用的 manager。

    用例要往进度缓冲里写数据时必须走同一个 manager，
    另造一个的话路由读的是另一个缓冲，测出来的「空结果」是假象。
    """

    manager: MonitorManager


@pytest.fixture()
def api(store: Store):  # noqa: ANN001
    manager = MonitorManager()
    handler = server_mod.build_handler(store, manager)
    server_mod._install_dispatch(handler)
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    out = _Api(f"http://127.0.0.1:{httpd.server_address[1]}")
    out.manager = manager
    yield out
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.fixture()
def auth_api(store: Store):  # noqa: ANN001
    """启用鉴权的服务，即绑到局域网时的形态（写接口要求显式令牌）。"""
    from campusnet.auth import ApiAuth

    manager = MonitorManager()
    auth = ApiAuth(token="t" * 43)
    handler = server_mod.build_handler(
        store, manager, auth=auth, auth_required=True
    )
    server_mod._install_dispatch(handler)
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    out = _Api(f"http://127.0.0.1:{httpd.server_address[1]}")
    out.manager = manager
    yield out
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive()


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


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/check"),
        ("POST", "/api/monitor/start"),
        ("POST", "/api/events/clear"),
        ("POST", "/api/accounts"),
        ("PUT", "/api/accounts/1"),
        ("DELETE", "/api/accounts/1"),
    ],
)
def test_write_routes_reject_foreign_origin(api, method, path):
    request = urllib.request.Request(
        str(api) + path,
        data=b"{}",
        method=method,
        headers={
            "Content-Type": "application/json",
            "Origin": "https://foreign.invalid",
        },
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == 403
    error.value.close()


EXTENSION_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


def test_write_allows_extension_origin_on_loopback(api) -> None:  # noqa: ANN001
    """浏览器扩展的写请求必须放行，否则「优先交给本地服务」永远不会生效。

    MV3 的 service worker 发非 GET 请求时浏览器会附上扩展来源
    （w3c/webextensions#777），它的 netloc 永远不等于 Host，因此之前必然 403；
    而扩展收到非 2xx 就静默回落——用户看到的是「插件运行正常」，
    实际上本地服务从来没被用过一次。
    """
    request = urllib.request.Request(
        f"{api}/api/events/clear",
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Origin": EXTENSION_ORIGIN,
            "Sec-Fetch-Site": "cross-site",
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200


def test_write_rejects_extension_origin_from_remote_host(api) -> None:  # noqa: ANN001
    """扩展来源只在 Host 是回环时放行：跨机访问仍然必须带令牌。"""
    request = urllib.request.Request(
        f"{api}/api/events/clear",
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Host": "10.0.0.9:2268",
            "Origin": EXTENSION_ORIGIN,
        },
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == 403
    error.value.close()


def test_write_allows_valid_token_from_foreign_origin(auth_api) -> None:  # noqa: ANN001
    """显式令牌有效时跳过同源校验：令牌不是浏览器自动附带的，跨站页伪造不出来。"""
    request = urllib.request.Request(
        f"{auth_api}/api/settings",
        data=b"{}",
        method="PUT",
        headers={
            "Content-Type": "application/json",
            "Origin": "https://evil.example",
            "X-CampusNet-Token": "t" * 43,
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200


def test_invalid_token_is_rejected_before_origin_check(auth_api) -> None:  # noqa: ANN001
    """豁免必须是「令牌有效」，不能退化成「带了头就放行」。

    无效令牌在鉴权那一层就被挡下（401），根本走不到同源校验——顺序本身就是
    一道保障：伪造的令牌不会因为「同源豁免」而拿到写权限。
    """
    request = urllib.request.Request(
        f"{auth_api}/api/settings",
        data=b"{}",
        method="PUT",
        headers={
            "Content-Type": "application/json",
            "Origin": "https://evil.example",
            "X-CampusNet-Token": "wrong-" + "t" * 38,
        },
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == 401
    error.value.close()


def test_extension_origin_still_needs_token_when_auth_required(auth_api) -> None:  # noqa: ANN001
    """绑到局域网时扩展必须带令牌——同源豁免不构成绕开鉴权的后门。"""
    request = urllib.request.Request(
        f"{auth_api}/api/events/clear",
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Origin": EXTENSION_ORIGIN,
        },
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == 401
    error.value.close()


@pytest.mark.parametrize(
    "origin", ["https://console.example", "http://console.example"]
)
def test_write_origin_supports_proxy_scheme(api, origin):
    request = urllib.request.Request(
        str(api) + "/api/settings",
        data=b"{}",
        method="PUT",
        headers={
            "Content-Type": "application/json",
            "Host": "console.example",
            "Origin": origin,
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200


def test_health(api: _Api) -> None:
    status, data = call(f"{api}/api/health")
    assert status == 200 and data["ok"] is True
    assert data["accounts"] == 0


def test_healthz(api: _Api) -> None:
    status, data = call(f"{api}/healthz")
    assert status == 200 and data["ok"] is True


def test_versioned_path_is_an_alias(api: _Api) -> None:
    """版本化别名：/api/v1/health 与 /api/health 是同一件事。"""
    plain = call(f"{api}/api/health")[1]

    status, data = call(f"{api}/api/v1/health")

    assert status == 200
    assert data["ok"] is True
    assert data["accounts"] == plain["accounts"]


def test_versioned_write_path_works(api: _Api) -> None:
    status, data = call(
        f"{api}/api/v1/accounts",
        "POST",
        {
            "name": "v1 路径",
            "portal_url": "http://10.0.0.1/eportal/portal/login",
            "username": "u",
            "password": "p",
        },
    )

    assert status == 201
    assert data["id"] > 0


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_bootstart_failure_is_http_error(api, monkeypatch, action):
    result = server_mod.bootstart.BootStatus(
        True, False, None, "simulated failure", False
    )
    monkeypatch.setattr(server_mod.bootstart, action, lambda **kwargs: result)
    status, data = call(f"{api}/api/bootstart/{action}", "POST", {})
    assert status == 500
    assert data["ok"] is False
    assert data["success"] is False
    assert data["error"] == "simulated failure"


@pytest.mark.parametrize(
    "payload",
    [{"port": "abc"}, {"port": 0}, {"port": True}, {"port": 1.5}, {"host": "bad host"}],
)
def test_bootstart_invalid_parameters(api, monkeypatch, payload):
    def unexpected(**kwargs):
        pytest.fail("invalid parameters must not install startup entry")

    monkeypatch.setattr(server_mod.bootstart, "install", unexpected)
    status, data = call(f"{api}/api/bootstart/install", "POST", payload)
    assert status == 400
    assert not data["ok"]


def test_bootstart_defaults_to_listener(api, monkeypatch):
    captured = {}

    def install(**kwargs):
        captured.update(kwargs)
        return server_mod.bootstart.BootStatus(True, True, None, "ok")

    monkeypatch.setattr(server_mod.bootstart, "install", install)
    assert call(f"{api}/api/bootstart/install", "POST", {})[0] == 200
    assert captured == {"host": "127.0.0.1", "port": int(api.rsplit(":", 1)[1])}


def test_power_action_requires_confirm(api, monkeypatch) -> None:  # noqa: ANN001
    """没有 confirm 就不能关机：防止误点，也防止跨站脚本一键断电。"""
    calls: list[str] = []
    monkeypatch.setattr(
        "campusnet.power.shutdown", lambda *a, **k: calls.append("off") or "cmd"
    )

    status, data = call(f"{api}/api/power/off", "POST", {})

    assert status == 400
    assert calls == []
    assert "确认" in data["error"]


def test_power_info_exposes_wake_instructions(api, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        "campusnet.power.wake_info",
        lambda: {
            "macs": ["AC:45:EF:49:B7:F4"],
            "command": "python utils/run.py power wake --mac AC:45:EF:49:B7:F4",
            "notes": ["BIOS 里设断电恢复为 Power On"],
        },
    )

    status, data = call(f"{api}/api/power/info")

    assert status == 200
    assert data["ok"] is True
    assert data["macs"] == ["AC:45:EF:49:B7:F4"]
    assert "power wake" in data["command"]


def test_power_action_executes_with_confirm(api, monkeypatch) -> None:  # noqa: ANN001
    calls: list[str] = []
    monkeypatch.setattr(
        "campusnet.power.shutdown",
        lambda *a, **k: calls.append("off") or "shutdown /s /t 0",
    )

    status, data = call(f"{api}/api/power/off", "POST", {"confirm": True})

    assert status == 200
    assert data["ok"] is True
    assert data["action"] == "off"
    assert data["command"] == "shutdown /s /t 0"
    assert calls == ["off"]


def test_power_action_reboot_and_unknown(api, monkeypatch) -> None:  # noqa: ANN001
    rebooted: list[str] = []
    monkeypatch.setattr(
        "campusnet.power.reboot", lambda *a, **k: rebooted.append("reboot") or "cmd"
    )

    ok_status, ok_data = call(f"{api}/api/power/reboot", "POST", {"confirm": True})
    bad_status, bad_data = call(f"{api}/api/power/format", "POST", {"confirm": True})

    assert ok_status == 200
    assert ok_data["action"] == "reboot"
    assert rebooted == ["reboot"]
    assert bad_status == 404
    assert bad_data["ok"] is False


def test_doctor_power_lists_items(api, monkeypatch) -> None:  # noqa: ANN001
    from campusnet import powerprep

    monkeypatch.setattr(
        "campusnet.powerprep.run",
        lambda fix=False, yes=False, plat=None: [
            powerprep.PowerItem("快速启动", powerprep.OK, "已关闭")
        ],
    )

    status, data = call(f"{api}/api/doctor/power")

    assert status == 200
    assert data["ok"] is True
    assert data["items"][0]["name"] == "快速启动"
    assert data["items"][0]["status"] == "ok"


def test_doctor_power_fix_requires_confirm(api, monkeypatch) -> None:  # noqa: ANN001
    calls: list = []
    monkeypatch.setattr("campusnet.powerprep.run", lambda **k: calls.append(k) or [])

    status, data = call(f"{api}/api/doctor/power/fix", "POST", {})

    assert status == 400
    assert calls == []
    assert "confirm" in data["error"]


def test_doctor_power_fix_executes(api, monkeypatch) -> None:  # noqa: ANN001
    calls: list = []
    monkeypatch.setattr(
        "campusnet.powerprep.run",
        lambda fix=False, yes=False, plat=None: calls.append({"fix": fix, "yes": yes}) or [],
    )

    status, data = call(f"{api}/api/doctor/power/fix", "POST", {"confirm": True})

    assert status == 200
    assert data["ok"] is True
    assert calls == [{"fix": True, "yes": False}]


def test_uninstall_consumes_body_on_persistent_connection(api, monkeypatch):
    import http.client

    result = server_mod.bootstart.BootStatus(
        True, True, None, "permission denied", False
    )
    monkeypatch.setattr(server_mod.bootstart, "uninstall", lambda: result)
    monkeypatch.setattr(server_mod.bootstart, "status", lambda: result)
    conn = http.client.HTTPConnection(
        "127.0.0.1", int(api.rsplit(":", 1)[1]), timeout=5
    )
    try:
        conn.request(
            "POST",
            "/api/bootstart/uninstall",
            "{}",
            {"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        assert response.status == 500
        response.read()
        conn.request("GET", "/api/bootstart")
        response = conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["installed"] is True
    finally:
        conn.close()


@pytest.mark.parametrize("action", ["install", "uninstall"])
@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"Content-Type": "text/plain"}, 415),
        ({"Content-Type": "application/json", "Origin": "http://evil.example"}, 403),
        ({"Content-Type": "application/json", "Origin": "null"}, 403),
        ({"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}, 403),
    ],
)
def test_bootstart_rejects_cross_site(api, monkeypatch, action, headers, expected):
    calls = []
    monkeypatch.setattr(
        server_mod.bootstart, action, lambda **kwargs: calls.append(kwargs)
    )
    request = urllib.request.Request(
        f"{api}/api/bootstart/{action}", data=b"{}", headers=headers
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == expected
    error.value.close()
    assert calls == []


def test_account_crud_flow(api: _Api) -> None:
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


def test_update_missing_account_404(api: _Api) -> None:
    status, data = call(
        f"{api}/api/accounts/999", "PUT", {"name": "x", "portal_url": "http://a"}
    )
    assert status == 404
    assert "不存在" in data["error"]


def test_create_validation_errors(api: _Api) -> None:
    status, data = call(f"{api}/api/accounts", "POST", {"portal_url": "http://a"})
    assert status == 400 and "名称" in data["error"]

    status, data = call(f"{api}/api/accounts", "POST", {"name": "a"})
    assert status == 400 and "门户地址" in data["error"]

    status, data = call(
        f"{api}/api/accounts", "POST", {"name": "a", "portal_url": "ftp://a"}
    )
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


def test_invalid_json_body(api: _Api) -> None:
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


def test_method_not_allowed(api: _Api) -> None:
    status, data = call(f"{api}/api/health", "POST", {})
    assert status == 405
    assert "GET" in data["error"]


def test_rejected_method_consumes_body_on_persistent_connection(api):
    import http.client

    conn = http.client.HTTPConnection(
        "127.0.0.1", int(api.rsplit(":", 1)[1]), timeout=5
    )
    try:
        for _ in range(3):
            conn.request(
                "POST", "/api/health", "{}", {"Content-Type": "application/json"}
            )
            response = conn.getresponse()
            assert response.status == 405
            response.read()
            conn.request("GET", "/api/health")
            response = conn.getresponse()
            assert response.status == 200
            assert json.loads(response.read())["ok"]
    finally:
        conn.close()


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Content-Length": "-1"}, 400),
        ({"Content-Length": "invalid"}, 400),
        ({"Content-Length": str(server_mod.MAX_REQUEST_BODY + 1)}, 413),
        ({"Transfer-Encoding": "chunked"}, 400),
    ],
)
def test_request_body_rejects_invalid_framing(api, headers, status):
    import http.client

    conn = http.client.HTTPConnection(
        "127.0.0.1", int(api.rsplit(":", 1)[1]), timeout=5
    )
    try:
        conn.request("POST", "/api/health", headers=headers)
        response = conn.getresponse()
        assert response.status == status
        assert json.loads(response.read())["ok"] is False
    finally:
        conn.close()


def test_request_body_timeout_is_bounded(api, monkeypatch):
    import http.client

    monkeypatch.setattr(server_mod, "BODY_READ_TIMEOUT", 0.1)
    conn = http.client.HTTPConnection(
        "127.0.0.1", int(api.rsplit(":", 1)[1]), timeout=5
    )
    try:
        conn.request("POST", "/api/health", headers={"Content-Length": "10"})
        response = conn.getresponse()
        assert response.status == 408
        response.read()
    finally:
        conn.close()


def test_request_body_truncated_is_rejected(api):
    import http.client
    import socket

    conn = http.client.HTTPConnection(
        "127.0.0.1", int(api.rsplit(":", 1)[1]), timeout=5
    )
    try:
        conn.request("POST", "/api/health", body=b"{", headers={"Content-Length": "10"})
        conn.sock.shutdown(socket.SHUT_WR)
        response = conn.getresponse()
        assert response.status == 400
        response.read()
    finally:
        conn.close()


def test_unknown_api_path_returns_json_404(api: _Api) -> None:
    status, data = call(f"{api}/api/nope")
    assert status == 404 and data["ok"] is False


def test_probe_endpoint(api: _Api) -> None:
    status, data = call(f"{api}/api/probe?url=http://127.0.0.1:9/x&timeout=2")
    assert status == 200
    assert "result" in data and isinstance(data["result"]["online"], bool)


def test_detect_portal_endpoint(api: _Api) -> None:
    status, data = call(f"{api}/api/detect-portal?url=http://127.0.0.1:9/x")
    assert status == 200
    assert "portal_url" in data


def test_discover_endpoint_without_scan(api: _Api) -> None:
    status, data = call(
        f"{api}/api/discover?seed=http://127.0.0.1:9/x&timeout=1&scan=0"
    )
    assert status == 200
    assert data["ok"] is True
    assert data["report"]["ok"] is False
    assert data["report"]["probe_candidates"]
    assert data["best"] is None
    assert data["account_fields"] is None


def test_discover_endpoint_fills_account_fields(
    api: _Api, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """探到门户时要把可直接建号的字段一并返回。"""
    from campusnet import discovery as discovery_mod

    candidate = discovery_mod.PortalCandidate(
        url="http://10.0.0.1/eportal/portal/login",
        status=200,
        title="Dr.COM",
        vendor="Dr.COM",
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
    assert data["best"]["vendor"] == "Dr.COM"
    assert data["account_fields"]["username_field"] == "DDDDD"
    assert data["account_fields"]["password_mode"] == "base64"
    assert data["account_fields"]["extra_fields"] == {"0MKKey": ""}


def test_login_endpoint_reports_missing_account(api: _Api) -> None:
    status, data = call(f"{api}/api/accounts/42/login", "POST", {})
    assert status == 404 and "不存在" in data["error"]


def test_login_endpoint_runs_manual_mode(
    api: _Api, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    from campusnet.portal import LoginResult

    monkeypatch.setattr(
        server_mod.portal,
        "login",
        lambda *a, **k: LoginResult("success", "已放行", "detail"),
    )
    _, created = call(
        f"{api}/api/accounts",
        "POST",
        {
            "name": "手动",
            "portal_url": "http://10.0.0.1/x",
            "username": "u",
            "password": "p",
        },
    )
    status, data = call(f"{api}/api/accounts/{created['id']}/login", "POST", {})
    assert status == 200
    assert data["state"] == "success" and data["message"] == "已放行"


def test_monitor_start_stop_and_status(api: _Api) -> None:
    status, data = call(f"{api}/api/monitor/start", "POST", {"interval": 300})
    assert status == 200 and data["status"]["interval"] == 300
    assert data["status"]["alive"] is True

    status, data = call(f"{api}/api/monitor")
    assert status == 200 and data["status"]["alive"] is True

    status, data = call(f"{api}/api/monitor/stop", "POST", {})
    assert status == 200 and data["stopped"] is True

    status, data = call(f"{api}/api/monitor")
    assert status == 200 and data["status"]["alive"] is False


def test_monitor_start_is_idempotent(api: _Api) -> None:
    _, first = call(f"{api}/api/monitor/start", "POST", {})
    _, second = call(f"{api}/api/monitor/start", "POST", {})
    assert first["started"] is True
    assert second["started"] is False
    call(f"{api}/api/monitor/stop", "POST", {})


def test_monitor_check_endpoint(api: _Api) -> None:
    status, data = call(f"{api}/api/check", "POST", {})
    assert status == 200
    assert data["results"] == []


def test_events_endpoint(api: _Api) -> None:
    _, created = call(
        f"{api}/api/accounts",
        "POST",
        {
            "name": "日志",
            "portal_url": "http://10.0.0.1/x",
            "username": "u",
            "password": "p",
        },
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


def test_settings_get_and_put(api: _Api) -> None:
    status, data = call(
        f"{api}/api/settings", "PUT", {"interval": 90, "headless": False}
    )
    assert status == 200 and data["settings"]["interval"] == 90

    status, data = call(f"{api}/api/settings")
    assert status == 200 and data["settings"]["headless"] is False


def test_static_index_is_served(api: _Api) -> None:
    with urllib.request.urlopen(f"{api}/", timeout=10) as resp:
        html = resp.read().decode("utf-8")
    assert resp.status == 200
    assert "校园网自连" in html


def test_static_missing_file_404(api: _Api) -> None:
    status, data = call(f"{api}/nope.html")
    assert status == 404 and data["ok"] is False


def test_path_traversal_blocked(api: _Api) -> None:
    status, _ = call(f"{api}/../config.json")
    assert status in (403, 404)


def test_server_exception_returns_500(
    api: _Api, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        server_mod.Store,
        "list_accounts",
        lambda self: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    status, data = call(f"{api}/api/accounts")
    assert status == 500
    assert "boom" in data["error"]


def test_account_password_kept_when_blank_on_update(api: _Api) -> None:
    _, created = call(
        f"{api}/api/accounts",
        "POST",
        {
            "name": "保留",
            "portal_url": "http://10.0.0.1/x",
            "username": "u",
            "password": "orig",
        },
    )
    status, _ = call(
        f"{api}/api/accounts/{created['id']}",
        "PUT",
        {
            "name": "改名",
            "portal_url": "http://10.0.0.1/x",
            "username": "u",
            "password": "",
        },
    )
    assert status == 200

    status, data = call(f"{api}/api/accounts")
    assert data["accounts"][0]["name"] == "改名"


# ---------- /api/progress 实时进度 ----------
# 前端每 900ms 拉一次，这个路由此前零测试覆盖。


def test_progress_empty_buffer(api: _Api) -> None:
    status, data = call(f"{api}/api/progress")
    assert status == 200
    assert data["ok"] is True
    assert data["items"] == []
    assert data["seq"] == 0
    assert data["checking"] is False


def test_progress_returns_emitted_items(api: _Api, store: Store) -> None:  # noqa: ANN001
    monitor = api.manager.ensure(store)
    account = Account(
        id=1, name="甲", portal_url="http://a", username="u", password="p"
    )
    monitor.emit_progress(account, "probe", "正在探测")
    monitor.emit_progress(account, "done", "已放行")

    status, data = call(f"{api}/api/progress")
    assert status == 200
    assert len(data["items"]) == 2
    assert data["seq"] == 2
    assert data["items"][0]["stage"] == "probe"
    assert data["items"][1]["message"] == "已放行"


def test_progress_seq_filter_is_incremental(api: _Api, store: Store) -> None:  # noqa: ANN001
    """带 seq 回来只拿新增部分，前端靠这个避免重复渲染。"""
    monitor = api.manager.ensure(store)
    account = Account(
        id=1, name="甲", portal_url="http://a", username="u", password="p"
    )
    for i in range(5):
        monitor.emit_progress(account, "probe", f"第 {i} 条")

    status, data = call(f"{api}/api/progress?seq=3")
    assert status == 200
    assert [i["message"] for i in data["items"]] == ["第 3 条", "第 4 条"]
    assert data["seq"] == 5


def test_progress_seq_zero_returns_all(api: _Api, store: Store) -> None:  # noqa: ANN001
    monitor = api.manager.ensure(store)
    account = Account(
        id=1, name="甲", portal_url="http://a", username="u", password="p"
    )
    monitor.emit_progress(account, "probe", "唯一一条")

    _, data = call(f"{api}/api/progress?seq=0")
    assert len(data["items"]) == 1


def test_progress_future_seq_returns_empty(api: _Api, store: Store) -> None:  # noqa: ANN001
    monitor = api.manager.ensure(store)
    account = Account(
        id=1, name="甲", portal_url="http://a", username="u", password="p"
    )
    monitor.emit_progress(account, "probe", "一条")

    _, data = call(f"{api}/api/progress?seq=999")
    assert data["items"] == []


def test_progress_filters_by_account(api: _Api, store: Store) -> None:  # noqa: ANN001
    monitor = api.manager.ensure(store)
    a1 = Account(id=1, name="甲", portal_url="http://a", username="u", password="p")
    a2 = Account(id=2, name="乙", portal_url="http://b", username="u", password="p")
    monitor.emit_progress(a1, "probe", "甲的进度")
    monitor.emit_progress(a2, "probe", "乙的进度")
    monitor.emit_progress(a1, "done", "甲结束了")

    _, data = call(f"{api}/api/progress?seq=0&account_id=1")
    assert [i["message"] for i in data["items"]] == ["甲的进度", "甲结束了"]


def test_progress_latest_mode_returns_tail(api: _Api, store: Store) -> None:  # noqa: ANN001
    monitor = api.manager.ensure(store)
    account = Account(
        id=1, name="甲", portal_url="http://a", username="u", password="p"
    )
    for i in range(20):
        monitor.emit_progress(account, "probe", f"第 {i} 条")

    _, data = call(f"{api}/api/progress?latest=1&limit=5")
    assert len(data["items"]) == 5
    assert data["items"][-1]["message"] == "第 19 条"


@pytest.mark.parametrize("flag", ["0", "false", ""])
def test_progress_latest_flag_false_falls_back_to_seq(
    api: _Api, store: Store, flag: str
) -> None:  # noqa: ANN001
    """latest=0 / false / 空 都应走增量模式，不能被当成真。"""
    monitor = api.manager.ensure(store)
    account = Account(
        id=1, name="甲", portal_url="http://a", username="u", password="p"
    )
    for i in range(5):
        monitor.emit_progress(account, "probe", f"第 {i} 条")

    _, data = call(f"{api}/api/progress?latest={flag}&seq=3")
    assert [i["message"] for i in data["items"]] == ["第 3 条", "第 4 条"]


def test_progress_rejects_non_integer_seq(api: _Api) -> None:
    status, data = call(f"{api}/api/progress?seq=abc")
    assert status == 400
    assert "seq" in data["error"]


def test_progress_rejects_non_integer_account_id(api: _Api) -> None:
    status, data = call(f"{api}/api/progress?account_id=xyz")
    assert status == 400
    assert "account_id" in data["error"]


def test_progress_blank_account_id_means_all(api: _Api, store: Store) -> None:  # noqa: ANN001
    """account_id 留空表示不过滤，不该报 400。"""
    monitor = api.manager.ensure(store)
    account = Account(
        id=1, name="甲", portal_url="http://a", username="u", password="p"
    )
    monitor.emit_progress(account, "probe", "一条")

    status, data = call(f"{api}/api/progress?account_id=")
    assert status == 200
    assert len(data["items"]) == 1


def test_progress_reports_checking_state(api: _Api, store: Store) -> None:  # noqa: ANN001
    """前端据此决定进度面板是否继续转。"""
    monitor = api.manager.ensure(store)
    status, data = call(f"{api}/api/progress")
    assert status == 200
    assert data["checking"] is monitor.running


def test_progress_items_carry_ts_for_display(api: _Api, store: Store) -> None:  # noqa: ANN001
    monitor = api.manager.ensure(store)
    account = Account(
        id=1, name="甲", portal_url="http://a", username="u", password="p"
    )
    monitor.emit_progress(account, "probe", "带时间戳")

    _, data = call(f"{api}/api/progress")
    item = data["items"][0]
    assert set(item) == {"account_id", "account_name", "stage", "message", "ts", "seq"}
    assert isinstance(item["ts"], (int, float))
