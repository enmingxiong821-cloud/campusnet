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


@pytest.fixture()
def proxy_api(store: Store):  # noqa: ANN001
    """声明了「前面有反向代理」的服务，Host 允许是外面的域名。

    默认（没这个声明）会按 DNS 重绑定挡掉同样形状的请求，
    见 test_write_rejects_foreign_host_without_proxy_trust。
    """
    manager = MonitorManager()
    handler = server_mod.build_handler(store, manager, trust_proxy=True)
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
def test_write_origin_supports_proxy_scheme(proxy_api, origin):
    request = urllib.request.Request(
        str(proxy_api) + "/api/settings",
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


def test_write_rejects_foreign_host_without_proxy_trust(api) -> None:  # noqa: ANN001
    """没声明反代时，Host 是外部域名的写请求要按 DNS 重绑定挡掉。

    这不是「反代用不了」的副作用，而是它的另一半：攻击者把自己的域名解析到
    127.0.0.1，浏览器眼里页面与被访问的服务同源，Origin 与 Host 自然相等，
    同源校验必然通过——这条请求与反代流量在服务端一模一样，
    所以只能靠部署者显式声明来区分。
    """
    request = urllib.request.Request(
        str(api) + "/api/power/off",
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Host": "evil.example:2268",
            "Origin": "http://evil.example:2268",
        },
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == 403
    error.value.close()


def test_read_rejects_foreign_host_without_proxy_trust(api) -> None:  # noqa: ANN001
    """GET 也要挡：重绑定下浏览器认为同源，读回的响应它拿得到手里。

    /api/settings 里有 webhook 的密钥，读得到就等于泄露出去。
    """
    request = urllib.request.Request(
        str(api) + "/api/settings",
        headers={"Host": "evil.example:2268"},
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == 403
    error.value.close()


def test_loopback_hosts_still_pass_without_proxy_trust(api) -> None:  # noqa: ANN001
    """本机自己的三种写法都要照常放行，别把正常访问一起挡了。"""
    for host in ("127.0.0.1", "localhost", "[::1]:2268"):
        request = urllib.request.Request(
            str(api) + "/api/health", headers={"Host": host}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200, host


# —— 跨站 GET ---------------------------------------------------------------
# 浏览器能自动发起的跨站请求不止 POST：`<img src>`、`<script src>`、
# `fetch(mode:'no-cors')` 都能打出一个 GET，而页面拿不到响应（同源策略挡着，
# 这里也不发 CORS 头）。但拿不到响应不等于做不成事——/api/probe、/api/discover
# 会去扫网段、连端口，/api/doctor/power 会跑 powercfg / PowerShell，
# 页面看不见结果也能借着它们驱动本机干活。这就是「盲打 CSRF」。


@pytest.mark.parametrize(
    "path",
    ["/api/probe", "/api/discover", "/api/detect-portal", "/api/doctor/power"],
)
def test_side_effecting_get_rejects_foreign_origin(api, path):  # noqa: ANN001
    """有副作用的 GET 与写请求一样要挡：这些接口不用读响应也能起作用。"""
    request = urllib.request.Request(
        str(api) + path, headers={"Origin": "https://foreign.invalid"}
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == 403
    error.value.close()


def test_get_rejects_cross_site_marker_without_origin(api) -> None:  # noqa: ANN001
    """跨站的子资源请求不带 Origin，只认 Origin 会漏掉一整类请求。

    `<script src="http://127.0.0.1:2268/api/discover">` 这种就是：没有 Origin，
    但浏览器一定附上 `Sec-Fetch-Site: cross-site`。
    """
    request = urllib.request.Request(
        f"{api}/api/discover", headers={"Sec-Fetch-Site": "cross-site"}
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == 403
    error.value.close()


def test_read_get_rejects_foreign_origin(api) -> None:  # noqa: ANN001
    """只读的 GET 也一起挡：跨站页确实读不到响应，但服务端没有理由给它开这个口子。"""
    request = urllib.request.Request(
        f"{api}/api/accounts", headers={"Origin": "https://foreign.invalid"}
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == 403
    error.value.close()


def test_foreign_origin_get_with_valid_token_is_allowed(auth_api) -> None:  # noqa: ANN001
    """令牌有效时 GET 也放行，与写请求同一套豁免。"""
    request = urllib.request.Request(
        f"{auth_api}/api/accounts",
        headers={"Origin": "https://evil.example", "X-CampusNet-Token": "t" * 43},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200


@pytest.mark.parametrize("path", ["/healthz", "/api/health", "/api/auth/status"])
def test_public_get_allowed_cross_site(api, path):  # noqa: ANN001
    """白名单里的探活与状态接口必须不受同源校验约束。

    Docker 的 HEALTHCHECK、扩展的存活探测、控制台启动前的「服务起来了没」都走它们，
    而扩展的 fetch 还会带上 cross-site 标记——一刀切会把这三条正常链路一起挡掉，
    而它们都没有副作用，挡下来也换不到安全性。
    """
    request = urllib.request.Request(
        str(api) + path,
        headers={"Origin": "https://foreign.invalid", "Sec-Fetch-Site": "cross-site"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200


def test_same_origin_get_still_allowed(api) -> None:  # noqa: ANN001
    """同源 GET 照常放行。浏览器同源请求本就不带 Origin，带了也必须通过——
    别把控制台自己的正常读取一起挡了。"""
    request = urllib.request.Request(
        f"{api}/api/accounts", headers={"Origin": str(api)}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200


def test_extension_origin_get_allowed_on_loopback(api) -> None:  # noqa: ANN001
    """扩展来源的 GET 同样放行：它读的是同一批接口，挡掉只会让扩展静默回落。

    这不是提权——任何本机进程本来就能不带令牌直连 127.0.0.1:2268（设计如此），
    而远端网页伪造不出 chrome-extension:// 这个 scheme。
    """
    request = urllib.request.Request(
        f"{api}/api/accounts",
        headers={"Origin": EXTENSION_ORIGIN, "Sec-Fetch-Site": "cross-site"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200


@pytest.mark.parametrize(
    "value, expected",
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("", False),
        ("0", False),
        ("no", False),
    ],
)
def test_trust_proxy_env_flag(monkeypatch, value: str, expected: bool) -> None:
    """开关只认明确的真值：写错的字符串（如 off 之外的任意文本）按关闭处理。"""
    monkeypatch.setenv("CAMPUSNET_TRUST_PROXY", value)
    assert server_mod._trust_proxy_enabled() is expected


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
    assert captured == {
        "host": "127.0.0.1",
        "port": int(api.rsplit(":", 1)[1]),
        "mode": "auto",
        "password": "",
    }


def test_bootstart_task_mode_forwards_password_without_recording_it(
    api, store: Store, monkeypatch  # noqa: ANN001
):
    """控制台注册计划任务：模式与密码送到 install()，密码不进响应也不进事件日志。

    密码是这一轮唯一新增的敏感输入——它只该存在于这一次请求里。
    """
    monkeypatch.setenv("CAMPUSNET_FORCE_PLATFORM", "windows")
    captured = {}

    def install(**kwargs):
        captured.update(kwargs)
        return server_mod.bootstart.BootStatus(
            True, True, None, "已注册计划任务（开机触发，无需登录即运行）"
        )

    monkeypatch.setattr(server_mod.bootstart, "install", install)

    status, data = call(
        f"{api}/api/bootstart/install", "POST", {"mode": "task", "password": "hunter2"}
    )

    assert status == 200
    assert captured["mode"] == "task"
    assert captured["password"] == "hunter2"
    assert "hunter2" not in json.dumps(data)
    events = json.dumps(store.list_events(limit=50), ensure_ascii=False)
    assert "hunter2" not in events
    assert "task" in events  # 载体还是记一笔，排查时用得上


def test_bootstart_task_mode_rejected_off_windows(api, monkeypatch):
    """另外两个平台的替代路径是系统级单元（要 root），控制台装不了。"""
    monkeypatch.setenv("CAMPUSNET_FORCE_PLATFORM", "linux")
    monkeypatch.setattr(
        server_mod.bootstart,
        "install",
        lambda **kwargs: pytest.fail("非 Windows 上不该走到 install()"),
    )

    status, data = call(
        f"{api}/api/bootstart/install", "POST", {"mode": "task", "password": "x"}
    )

    assert status == 400
    assert "只支持 Windows" in data["error"]


@pytest.mark.parametrize(
    "payload",
    [{"mode": "weird"}, {"mode": "task", "password": 123}, {"mode": None}],
)
def test_bootstart_rejects_bad_mode_payloads(api, monkeypatch, payload):
    monkeypatch.setenv("CAMPUSNET_FORCE_PLATFORM", "windows")
    monkeypatch.setattr(
        server_mod.bootstart,
        "install",
        lambda **kwargs: pytest.fail("参数不对时不该走到 install()"),
    )

    status, data = call(f"{api}/api/bootstart/install", "POST", payload)

    assert status == 400
    assert not data["ok"]


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


def test_wake_plans_crud(api) -> None:  # noqa: ANN001
    status, data = call(
        f"{api}/api/wake-plans",
        "POST",
        {"name": "每天开机", "mac": "AA:BB:CC:DD:EE:FF", "schedule": "07:30"},
    )
    assert status == 201
    plan_id = data["id"]

    status, data = call(f"{api}/api/wake-plans")
    assert status == 200
    assert len(data["plans"]) == 1
    assert data["plans"][0]["name"] == "每天开机"

    status, data = call(f"{api}/api/wake-plans/{plan_id}", "DELETE")
    assert status == 200

    status, data = call(f"{api}/api/wake-plans")
    assert data["plans"] == []


def test_wake_plan_validation(api) -> None:  # noqa: ANN001
    status, _ = call(
        f"{api}/api/wake-plans",
        "POST",
        {"name": "x", "mac": "bad-mac", "schedule": "07:30"},
    )
    assert status == 400

    status, _ = call(
        f"{api}/api/wake-plans",
        "POST",
        {"name": "x", "mac": "AA:BB:CC:DD:EE:FF", "schedule": "25:00"},
    )
    assert status == 400


def test_devices_crud_and_wake(api, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        "campusnet.power.wake",
        lambda mac, broadcast="255.255.255.255", port=9: f"{broadcast}:{port}",
    )

    status, data = call(
        f"{api}/api/devices",
        "POST",
        {"name": "树莓派", "mac": "11:22:33:44:55:66", "note": "跳板"},
    )
    assert status == 201
    device_id = data["id"]

    status, data = call(f"{api}/api/devices")
    assert len(data["devices"]) == 1

    status, data = call(f"{api}/api/devices/{device_id}/wake", "POST", {})
    assert status == 200
    assert data["ok"] is True

    status, data = call(f"{api}/api/devices/{device_id}", "DELETE")
    assert status == 200


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


def test_settings_rejects_unknown_key(api: _Api) -> None:
    """不在白名单里的键要 400，而不是悄悄落库。

    没有白名单时任何 JSON 都能写进 settings 表，而 MonitorManager.ensure()
    会 `int(get_setting("interval"))`——一条 {"interval": "abc"} 就够让服务
    从此起不来，坏值在库里，重启也没用。
    """
    status, data = call(f"{api}/api/settings", "PUT", {"whatever": 1})
    assert status == 400
    assert "不可写" in data["error"]


@pytest.mark.parametrize(
    "payload",
    [
        {"interval": "abc"},
        {"interval": True},
        {"interval": 1.5},
        {"headless": "yes"},
        {"max_events": None},
        {"notify_url": 123},
        {"notify_events": "failing"},
        {"notify_events": [1, 2]},
    ],
)
def test_settings_rejects_bad_type(api: _Api, payload: dict) -> None:
    """类型不对一律 400。

    True 单列一条：bool 是 int 的子类，不单独判就会当成 1 悄悄写进去。
    """
    status, data = call(f"{api}/api/settings", "PUT", payload)
    assert status == 400, payload
    assert data["ok"] is False


def test_rejected_setting_never_reaches_the_store(api: _Api, store: Store) -> None:
    """校验要在落库之前，且同一请求里合法的键也不能先写进去一半。"""
    status, _ = call(
        f"{api}/api/settings", "PUT", {"headless": True, "interval": "abc"}
    )
    assert status == 400
    assert store.get_setting("interval", None) is None, "坏值不该落库"
    assert store.get_setting("headless", None) is None, "整批都该被拒，不能写一半"


def test_static_index_is_served(api: _Api) -> None:
    with urllib.request.urlopen(f"{api}/", timeout=10) as resp:
        html = resp.read().decode("utf-8")
    assert resp.status == 200
    assert "校园网自连" in html


def test_static_missing_file_404(api: _Api) -> None:
    status, data = call(f"{api}/nope.html")
    assert status == 404 and data["ok"] is False


def test_pwa_assets_served(api: _Api) -> None:
    with urllib.request.urlopen(f"{api}/manifest.webmanifest", timeout=10) as resp:
        manifest = resp.read().decode("utf-8")
        manifest_ctype = resp.headers.get("Content-Type", "")
    assert resp.status == 200
    assert "application/manifest+json" in manifest_ctype
    assert '"display": "standalone"' in manifest

    with urllib.request.urlopen(f"{api}/service-worker.js", timeout=10) as resp:
        sw = resp.read().decode("utf-8")
        sw_ctype = resp.headers.get("Content-Type", "")
    assert resp.status == 200
    assert "javascript" in sw_ctype
    assert "campusnet-shell" in sw

    with urllib.request.urlopen(f"{api}/icons/icon192.png", timeout=10) as resp:
        raw = resp.read()
        png_ctype = resp.headers.get("Content-Type", "")
    assert resp.status == 200
    assert "image/png" in png_ctype
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"


def test_pair_generate_and_claim(api: _Api) -> None:
    status, data = call(f"{api}/api/pair", "POST", {})
    assert status == 200
    code = data["code"]
    assert len(code) == 6
    assert data["url"].endswith("#pair=" + code)

    status, data = call(f"{api}/api/pair/claim", "POST", {"code": code})
    assert status == 200
    assert data["ok"] is True

    # 单次使用：第二次报 401
    status, data = call(f"{api}/api/pair/claim", "POST", {"code": code})
    assert status == 401


def test_pair_claim_rejects_wrong_code(api: _Api) -> None:
    status, data = call(f"{api}/api/pair/claim", "POST", {"code": "ZZZZZZ"})
    assert status == 401


def test_pair_claim_requires_code(api: _Api) -> None:
    status, _ = call(f"{api}/api/pair/claim", "POST", {})
    assert status == 400


def test_pair_claim_sets_session_cookie(auth_api: _Api) -> None:
    # 用显式令牌生成配对码（/api/pair 需要认证）
    req = urllib.request.Request(
        f"{auth_api}/api/pair",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json", "X-CampusNet-Token": "t" * 43},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        code = json.loads(resp.read().decode("utf-8"))["code"]

    # claim 是公开接口，凭码换会话 Cookie
    req = urllib.request.Request(
        f"{auth_api}/api/pair/claim",
        data=json.dumps({"code": code}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        cookie = resp.headers.get("Set-Cookie", "")

    assert "campusnet_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie


def test_notify_channels_listed(api: _Api) -> None:
    status, data = call(f"{api}/api/notify/channels")
    assert status == 200
    assert data["channels"]["bark"] == "Bark"
    assert data["channels"]["wecom"] == "企业微信机器人"


def test_notify_test_requires_config(api: _Api) -> None:
    status, _ = call(f"{api}/api/notify/test", "POST", {})
    assert status == 400


def test_notify_test_sends_when_configured(api: _Api, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("campusnet.notify.send_test", lambda notifier: True)
    call(f"{api}/api/settings", "PUT", {"notify_channel": "bark", "notify_key": "k"})

    status, data = call(f"{api}/api/notify/test", "POST", {})

    assert status == 200
    assert data["ok"] is True


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


# ---------- 请求体边界 ----------


def test_empty_body_is_treated_as_empty_object(api: _Api) -> None:
    """无请求体时 _read_body 返回空字典，而不是报错。"""
    status, data = call(f"{api}/api/settings", "PUT")
    assert status == 200 and data["ok"] is True


def test_non_object_json_body_is_rejected(api: _Api) -> None:
    """合法 JSON 但不是对象（数组/字符串）也要 400。"""
    req = urllib.request.Request(
        f"{api}/api/settings", data=b"[1, 2, 3]", method="PUT"
    )
    req.add_header("Content-Type", "application/json")
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(req, timeout=10)
    assert error.value.code == 400
    assert "JSON 对象" in error.value.read().decode("utf-8")
    error.value.close()


def test_duplicate_content_length_is_rejected(api: _Api) -> None:
    """重复的 Content-Length 头必须拒绝（请求走私防御）。"""
    import socket

    host, port = "127.0.0.1", int(api.rsplit(":", 1)[1])
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(
            b"POST /api/health HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Length: 0\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        raw = sock.recv(4096)
    assert b"400" in raw


def test_request_target_without_leading_slash_cannot_skip_auth(
    auth_api: _Api,
) -> None:
    """request-target 不带前导斜杠，不能绕过鉴权与回环校验。

    RFC 9112 要求 origin-form 以 `/` 开头，但 stdlib 的 `parse_request` 不校验
    （只把开头的 `//` 折叠成一个 `/`）。而路由匹配用的是 `strip("/")`，于是
    `GET api/settings` 照样命中模板，却绕开所有 `path.startswith("/api/")` 的
    门禁——鉴权与 Host 回环校验一起跳过，服务绑到 `0.0.0.0` 时局域网内零令牌
    就能读设置（含 notify_key）、读账号、关机。
    """
    import socket

    def raw_get(target: str, token: str = "") -> bytes:
        host, port = "127.0.0.1", int(auth_api.rsplit(":", 1)[1])
        auth = f"Authorization: Bearer {token}\r\n" if token else ""
        request = f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1\r\n{auth}Connection: close\r\n\r\n"
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(request.encode("ascii"))
            return sock.recv(4096).split(b"\r\n", 1)[0]

    for target in ("api/settings", "//api/settings"):
        assert b" 401 " in raw_get(target), target

    # 同样这条斜杠缺失的路径，带上真令牌就该正常工作：证明 401 来自鉴权门，
    # 而不是这条路径被归一化改坏了（否则「一律 401」也能骗过上面两行）。
    assert b" 200 " in raw_get("api/settings", token="t" * 43)


# ---------- 旧版 _dispatch 分支 ----------


def test_base_dispatch_handles_all_methods(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不装 _install_dispatch 时，ApiHandler._dispatch / do_* 仍是活代码路径。"""
    from http.server import ThreadingHTTPServer

    handler = server_mod.build_handler(store, MonitorManager())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        # 命中路由
        status, data = call(f"{base}/api/health")
        assert status == 200 and data["ok"] is True
        # 路径不存在且没有同名路由 -> 走静态资源 -> 404
        status, data = call(f"{base}/api/nope")
        assert status == 404 and data["ok"] is False
        # 路径存在但方法不匹配 -> 405
        for method in ("POST", "PUT", "DELETE"):
            status, data = call(f"{base}/api/health", method, {})
            assert status == 405 and data["ok"] is False
        # 处理器主动抛 ApiError -> 原样返回状态码
        status, data = call(f"{base}/api/accounts", "POST", {})
        assert status == 400
        # 处理器抛非 ApiError -> 兜底 500，且记一条事件
        # 载体是「让 store 抛」：以前这里借的是「timeout 传非法值会 ValueError」，
        # 而那个参数已经改成夹取，用例会静默变成在测 200，测不到兜底。
        monkeypatch.setattr(store, "list_accounts", lambda: 1 / 0)
        status, data = call(f"{base}/api/health")
        assert status == 500 and "ZeroDivisionError" in data["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# ---------- 直接调用路由函数（参数缺失时无法经 HTTP 到达） ----------


def _route_fn(store: Store, key: tuple[str, str]):  # noqa: ANN202
    handler = server_mod.build_handler(store, MonitorManager())
    return handler.routes[key]


def test_delete_account_without_id_param(store: Store) -> None:
    """缺少 params（旧分发器不注入模板参数）时报「缺少账号 ID」。"""
    fn = _route_fn(store, ("DELETE", "/api/accounts/{id}"))
    with pytest.raises(server_mod.ApiError) as error:
        fn({"query": {}, "path": "/api/accounts/1", "params": {}}, None)
    assert error.value.status == 400
    assert "缺少账号 ID" in error.value.message


def test_delete_wake_plan_without_id_param(store: Store) -> None:
    fn = _route_fn(store, ("DELETE", "/api/wake-plans/{id}"))
    with pytest.raises(server_mod.ApiError) as error:
        fn({"query": {}, "path": "/api/wake-plans/1", "params": {}}, None)
    assert error.value.status == 400
    assert "缺少计划 ID" in error.value.message


def test_bootstart_install_rejects_non_tcp_address(store: Store) -> None:
    """监听地址不是 TCP 元组（如 unix socket）时返回 500。"""
    import types

    fn = _route_fn(store, ("POST", "/api/bootstart/install"))

    class _Stub:
        headers = {"Content-Type": "application/json"}
        close_connection = False
        server = types.SimpleNamespace(server_address="unix-socket")

        def _read_body(self):
            return {}

    with pytest.raises(server_mod.ApiError) as error:
        fn({"query": {}, "path": "/api/bootstart/install"}, _Stub())
    assert error.value.status == 500


# ---------- 通知旁路失败不影响主流程 ----------


def test_power_action_swallows_notify_failure(api: _Api, monkeypatch) -> None:  # noqa: ANN001
    """通知渠道挂了也必须把电源动作结果正常返回。"""
    monkeypatch.setattr("campusnet.power.shutdown", lambda *a, **k: "cmd")

    class _Boom:
        @staticmethod
        def from_store(store):  # noqa: ANN001, ANN205
            raise RuntimeError("通知渠道不可用")

    monkeypatch.setattr("campusnet.notify.Notifier", _Boom)

    status, data = call(f"{api}/api/power/off", "POST", {"confirm": True})
    assert status == 200 and data["ok"] is True


# ---------- 各错误分支 ----------


def test_delete_account_non_integer_id(api: _Api) -> None:
    status, data = call(f"{api}/api/accounts/abc", "DELETE")
    assert status == 400 and data["ok"] is False


def test_login_with_non_integer_account_id(api: _Api) -> None:
    status, data = call(f"{api}/api/accounts/abc/login", "POST", {})
    assert status == 400 and "非法" in data["error"]


def test_monitor_start_applies_headless_and_autostart(api: _Api) -> None:
    status, data = call(
        f"{api}/api/monitor/start",
        "POST",
        {"interval": 120, "headless": True, "autostart": True},
    )
    assert status == 200 and data["status"]["alive"] is True
    call(f"{api}/api/monitor/stop", "POST", {})


def test_power_action_reports_power_error(api: _Api, monkeypatch) -> None:  # noqa: ANN001
    from campusnet import power as power_mod

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise power_mod.PowerError("拒绝访问")

    monkeypatch.setattr("campusnet.power.shutdown", _boom)
    status, data = call(f"{api}/api/power/off", "POST", {"confirm": True})
    assert status == 500
    assert "拒绝访问" in data["error"]


def test_notify_test_reports_send_failure(api: _Api, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr("campusnet.notify.send_test", lambda notifier: False)
    call(f"{api}/api/settings", "PUT", {"notify_channel": "bark", "notify_key": "k"})

    status, data = call(f"{api}/api/notify/test", "POST", {})
    assert status == 502 and data["ok"] is False


def test_wake_plan_requires_all_fields(api: _Api) -> None:
    status, _ = call(f"{api}/api/wake-plans", "POST", {"name": "只有名字"})
    assert status == 400


def test_wake_plan_rejects_non_integer_port(api: _Api) -> None:
    status, _ = call(
        f"{api}/api/wake-plans",
        "POST",
        {
            "name": "x",
            "mac": "AA:BB:CC:DD:EE:FF",
            "schedule": "07:30",
            "port": "abc",
        },
    )
    assert status == 400


def test_wake_plan_delete_non_integer_id(api: _Api) -> None:
    status, _ = call(f"{api}/api/wake-plans/abc", "DELETE")
    assert status == 400


def test_wake_plan_delete_missing_returns_404(api: _Api) -> None:
    status, data = call(f"{api}/api/wake-plans/999", "DELETE")
    assert status == 404 and data["ok"] is False


def test_device_requires_name_and_mac(api: _Api) -> None:
    status, _ = call(f"{api}/api/devices", "POST", {"name": "只有名字"})
    assert status == 400


def test_device_rejects_bad_mac(api: _Api) -> None:
    status, _ = call(f"{api}/api/devices", "POST", {"name": "x", "mac": "not-a-mac"})
    assert status == 400


def test_delete_device_missing_returns_404(api: _Api) -> None:
    status, data = call(f"{api}/api/devices/999", "DELETE")
    assert status == 404 and data["ok"] is False


def test_wake_device_missing_returns_404(api: _Api) -> None:
    status, data = call(f"{api}/api/devices/999/wake", "POST", {})
    assert status == 404 and data["ok"] is False


def test_wake_device_reports_power_error(api: _Api, monkeypatch) -> None:  # noqa: ANN001
    from campusnet import power as power_mod

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise power_mod.PowerError("广播失败")

    monkeypatch.setattr("campusnet.power.wake", _boom)
    _, created = call(
        f"{api}/api/devices", "POST", {"name": "树莓派", "mac": "11:22:33:44:55:66"}
    )
    status, data = call(f"{api}/api/devices/{created['id']}/wake", "POST", {})
    assert status == 400
    assert "广播失败" in data["error"]


# ---------- 配对码：过期与锁定 ----------


def test_pair_claim_rejects_expired_code(api: _Api, store: Store) -> None:  # noqa: ANN001
    from campusnet import pair as pair_mod

    code = "ABC123"
    store.add_pair_code(pair_mod.code_hash(code), "1970-01-01 00:00:00")
    status, data = call(f"{api}/api/pair/claim", "POST", {"code": code})
    assert status == 401
    assert "过期" in data["error"]


def test_pair_claim_locked_after_repeated_failures(
    api: _Api, store: Store
) -> None:  # noqa: ANN001
    from campusnet import pair as pair_mod

    code = "XYZ789"
    chash = pair_mod.code_hash(code)
    store.add_pair_code(chash, "2999-01-01 00:00:00")
    for _ in range(pair_mod.MAX_FAILURES):
        store.record_pair_failure(chash)

    status, data = call(f"{api}/api/pair/claim", "POST", {"code": code})
    assert status == 429
    assert data["ok"] is False


# ---------- /api/auth/* ----------


def test_auth_status_without_auth(api: _Api) -> None:
    status, data = call(f"{api}/api/auth/status")
    assert status == 200
    assert data["required"] is False
    assert data["authenticated"] is True


def test_auth_login_without_auth_is_noop(api: _Api) -> None:
    """未启用鉴权时登录接口直接放行。"""
    status, data = call(f"{api}/api/auth/login", "POST", {})
    assert status == 200
    assert data["required"] is False


def test_auth_login_rejects_wrong_token(auth_api: _Api) -> None:
    status, data = call(f"{auth_api}/api/auth/login", "POST", {"token": "wrong"})
    assert status == 401 and data["ok"] is False


def test_auth_login_accepts_correct_token(auth_api: _Api) -> None:
    status, data = call(f"{auth_api}/api/auth/login", "POST", {"token": "t" * 43})
    assert status == 200
    assert data["authenticated"] is True


def test_auth_logout_clears_session(auth_api: _Api) -> None:
    status, data = call(f"{auth_api}/api/auth/logout", "POST", {})
    assert status == 200 and data["ok"] is True


# ---------- 来源校验的边界 ----------


def test_extension_origin_from_bracketed_loopback_host(api: _Api) -> None:
    """Host 为 [::1]（带方括号的 IPv6）时也算回环，扩展写请求放行。"""
    request = urllib.request.Request(
        f"{api}/api/events/clear",
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Host": "[::1]:2268",
            "Origin": EXTENSION_ORIGIN,
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200


def test_malformed_origin_is_rejected(api: _Api) -> None:
    """Origin 无法解析（如残缺的 IPv6）时按跨来源拒绝。"""
    request = urllib.request.Request(
        f"{api}/api/settings",
        data=b"{}",
        method="PUT",
        headers={"Content-Type": "application/json", "Origin": "http://[::1"},
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == 403
    error.value.close()


# ---------- CORS 预检 ----------


def test_options_preflight_is_handled(api: _Api) -> None:
    request = urllib.request.Request(f"{api}/api/health", method="OPTIONS")
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 204
        assert "GET" in response.headers.get("Allow", "")
        assert response.headers.get("Access-Control-Allow-Methods")


# ---------- serve(): 启动、自启、退出清理 ----------


class _FakeLock:
    """替代 SingleInstance，记录释放次数。"""

    def __init__(self, path):  # noqa: ANN001
        self.path = path
        self.releases = 0

    def acquire(self) -> bool:
        return True

    def holder_pid(self):  # noqa: ANN201
        return 4321

    def release(self) -> None:
        self.releases += 1


class _FakeManager:
    """替代 MonitorManager，避免真的起监控线程。"""

    def __init__(self) -> None:
        self.started = False
        self.stopped = 0

    def start(self, store) -> None:  # noqa: ANN001
        self.started = True

    def stop(self) -> None:
        self.stopped += 1


def _patch_serve_env(monkeypatch, lock, manager, server_cls) -> None:  # noqa: ANN001
    monkeypatch.setattr(server_mod, "stdout_ready", lambda: None)
    monkeypatch.setattr(server_mod, "SingleInstance", lambda path: lock)
    monkeypatch.setattr(server_mod, "MonitorManager", lambda: manager)
    monkeypatch.setattr(server_mod, "ApiAuth", lambda *a, **k: object())
    monkeypatch.setattr(
        server_mod.ports, "resolve_ports", lambda spec, **kw: {p: p for p in spec}
    )
    monkeypatch.setattr(server_mod, "ThreadingHTTPServer", server_cls)


class _BlockingServer:
    """serve_forever 立刻抛 KeyboardInterrupt，模拟 Ctrl+C。"""

    def __init__(self, address, handler):  # noqa: ANN001
        self.server_address = address
        self.daemon_threads = False
        self.closed = False

    def serve_forever(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise KeyboardInterrupt

    def shutdown(self) -> None:
        pass

    def server_close(self) -> None:
        self.closed = True


def test_serve_exits_when_instance_running(store: Store, monkeypatch, capsys) -> None:  # noqa: ANN001
    class _BusyLock(_FakeLock):
        def acquire(self) -> bool:
            return False

    lock = _BusyLock("x")
    _patch_serve_env(monkeypatch, lock, _FakeManager(), _BlockingServer)

    with pytest.raises(SystemExit) as error:
        server_mod.serve(store=store, host="127.0.0.1", port=12345)

    assert error.value.code == 1
    assert "已有实例运行" in capsys.readouterr().err
    assert lock.releases == 0


def test_serve_exits_when_port_unavailable(store: Store, monkeypatch, capsys) -> None:  # noqa: ANN001
    lock = _FakeLock("x")

    def _boom(spec, **kwargs):  # noqa: ANN001, ANN003
        raise OSError("端口被占用")

    _patch_serve_env(monkeypatch, lock, _FakeManager(), _BlockingServer)
    monkeypatch.setattr(server_mod.ports, "resolve_ports", _boom)

    with pytest.raises(SystemExit) as error:
        server_mod.serve(store=store, host="127.0.0.1", port=12345)

    assert error.value.code == 1
    assert "端口被占用" in capsys.readouterr().err
    assert lock.releases == 1


def test_serve_exits_when_bind_fails(store: Store, monkeypatch, capsys) -> None:  # noqa: ANN001
    lock = _FakeLock("x")

    class _BadServer:
        def __init__(self, address, handler):  # noqa: ANN001
            raise OSError("绑定失败")

    _patch_serve_env(monkeypatch, lock, _FakeManager(), _BadServer)

    with pytest.raises(SystemExit) as error:
        server_mod.serve(store=store, host="127.0.0.1", port=12345)

    assert error.value.code == 1
    assert "绑定失败" in capsys.readouterr().err
    assert lock.releases == 1


def test_serve_blocking_stops_cleanly_on_interrupt(
    store: Store, monkeypatch, capsys
) -> None:  # noqa: ANN001
    """阻塞模式遇 Ctrl+C 后必须停监控、关服务、释放单实例锁。"""
    lock = _FakeLock("x")
    manager = _FakeManager()
    created: dict = {}

    class _InterruptServer(_BlockingServer):
        def __init__(self, address, handler):  # noqa: ANN001
            super().__init__(address, handler)
            created["server"] = self

    _patch_serve_env(monkeypatch, lock, manager, _InterruptServer)

    # 非回环地址 -> 启用鉴权，覆盖打印令牌提示的分支
    httpd = server_mod.serve(store=store, host="10.0.0.9", port=12345, autostart=True)

    assert httpd is created["server"]
    assert manager.started is True
    assert manager.stopped >= 1
    assert httpd.closed is True
    assert lock.releases == 1
    out = capsys.readouterr().out
    assert "正在停止" in out
    assert "令牌保护" in out


def test_serve_non_blocking_releases_on_shutdown(
    store: Store, monkeypatch
) -> None:  # noqa: ANN001
    """非阻塞模式返回 httpd；shutdown/server_close 各自触发一次释放。"""
    lock = _FakeLock("x")
    manager = _FakeManager()

    class _NoopServer:
        def __init__(self, address, handler):  # noqa: ANN001
            self.server_address = address
            self.daemon_threads = False
            self.shutdown_called = False

        def serve_forever(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return

        def shutdown(self) -> None:
            self.shutdown_called = True

        def server_close(self) -> None:
            self.closed = True

    _patch_serve_env(monkeypatch, lock, manager, _NoopServer)

    httpd = server_mod.serve(store=store, host="127.0.0.1", port=12345, blocking=False)

    assert httpd.shutdown_called is False  # serve 只是包了一层，不会自动调用
    httpd.shutdown()
    httpd.server_close()
    assert manager.stopped >= 1
    assert lock.releases == 1


# ---------- 参数边界与配对码限速 ----------


def test_pair_claim_throttles_missing_codes(api: _Api) -> None:
    """猜不存在的码原本不计数，等于给在线暴力猜解开了绿灯。"""
    from campusnet import pair as pair_mod

    server_mod._PAIR_SOURCE_FAILURES.clear()
    try:
        statuses = [
            call(f"{api}/api/pair/claim", "POST", {"code": f"AAAAA{i}"})[0]
            for i in range(pair_mod.MAX_FAILURES)
        ]
    finally:
        # 这张表是模块级的，不清会串到别的用例上
        server_mod._PAIR_SOURCE_FAILURES.clear()

    assert statuses[:-1] == [401] * (pair_mod.MAX_FAILURES - 1)
    assert statuses[-1] == 429


def test_events_limit_is_clamped(api: _Api) -> None:
    """limit 传非法值不报错，夹到区间内照常返回。"""
    status, data = call(f"{api}/api/events?limit=abc")

    assert status == 200 and data["ok"] is True


def test_create_wake_plan_rejects_out_of_range_port(api: _Api) -> None:
    status, data = call(
        f"{api}/api/wake-plans",
        "POST",
        {
            "name": "早八",
            "mac": "AA:BB:CC:DD:EE:FF",
            "schedule": "07:30",
            "port": 70000,
        },
    )

    assert status == 400 and "65535" in data["error"]
