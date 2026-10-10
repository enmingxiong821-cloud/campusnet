"""用 mock_portal 跑真实链路端到端测试。

与 test_portal.py 的区别：那边喂 HTML 字符串验证解析，
这边起两个真实 HTTP 服务，走完整的 _fetch → 表单解析 → 提交 → 复检链路。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

UTILS = Path(__file__).resolve().parents[3] / "utils"
if str(UTILS) not in sys.path:
    sys.path.insert(0, str(UTILS))

from campusnet.monitor import Monitor  # noqa: E402
from campusnet.portal import LoginResult, http_login, parse_forms, pick_login_form  # noqa: E402
from campusnet.probe import probe  # noqa: E402
from campusnet.storage import Account  # noqa: E402

import mock_portal  # noqa: E402


@pytest.fixture()
def mock_net():
    """起一套本地假校园网，跑完自动关掉。"""
    portal_port, probe_port = 2380, 2381
    import threading

    mock_portal.PORTAL_PORT = portal_port
    mock_portal.PROBE_PORT = probe_port
    mock_portal.PAGES["login"] = mock_portal.LOGIN_PAGE
    mock_portal.PAGES["admin"] = mock_portal.ADMIN_PAGE.replace("__USER__", mock_portal.USERNAME).replace(
        "__PASS__", mock_portal.PASSWORD
    )
    mock_portal.STATE["online"] = False

    from http.server import ThreadingHTTPServer

    portal = ThreadingHTTPServer(("127.0.0.1", portal_port), mock_portal.PortalHandler)
    probe = ThreadingHTTPServer(("127.0.0.1", probe_port), mock_portal.ProbeHandler)
    for server in (portal, probe):
        server.daemon_threads = True
    threads = []
    for server in (portal, probe):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)

    yield (
        f"http://127.0.0.1:{portal_port}/eportal/portal/login",
        f"http://127.0.0.1:{probe_port}/probe",
    )

    portal.shutdown()
    probe.shutdown()
    portal.server_close()
    probe.server_close()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()


def _account(portal: str, probe_url: str, password: str) -> Account:
    return Account(
        name="模拟",
        portal_url=portal,
        probe_url=probe_url,
        username=mock_portal.USERNAME,
        password=password,
        password_mode="base64",
        login_mode="http",
    )


# ---------- 探活 ----------


def test_probe_reports_offline_when_dropped(mock_net) -> None:  # noqa: ANN001
    mock_portal.STATE["online"] = False
    result = probe(mock_net[1], timeout=5)
    assert result.online is False
    assert result.status == "redirected"
    assert "eportal" in result.detail


def test_probe_reports_online_when_released(mock_net) -> None:  # noqa: ANN001
    mock_portal.STATE["online"] = True
    result = probe(mock_net[1], timeout=5)
    assert result.online is True
    assert "Microsoft Connect Test" in result.detail


# ---------- 表单解析（GB2312 真实响应） ----------


def test_parse_gb2312_login_form(mock_net) -> None:  # noqa: ANN001
    """页面是 GB2312 编码，若解码错误字段名会识别不出来。"""
    from campusnet.portal import _fetch, _opener
    from http.cookiejar import CookieJar

    status, body, _url, headers = _fetch(_opener(CookieJar()), mock_net[0])
    assert status == 200
    assert "gb2312" in headers.get("content-type", "")

    # 中文必须解对
    assert "校园网用户认证" in body
    assert "认证系统" in body

    form = pick_login_form(parse_forms(body))
    assert form is not None
    assert form.username_field == "DDDDD"
    assert form.password_field == "upass"
    assert form.method == "POST"
    # 隐藏域必须一起识别出来，少一个门户都会拒
    assert set(form.fields) == {"DDDDD", "upass", "0MKKey", "user_ip", "user_mac"}


# ---------- 登录链路 ----------


def test_login_success_releases_network(mock_net) -> None:  # noqa: ANN001
    """完整链路：掉线 → 提交 → 认证通过 → 复检放行。"""
    mock_portal.STATE["online"] = False
    result = http_login(_account(mock_net[0], mock_net[1], mock_portal.PASSWORD))

    assert result.state == "success"
    assert "放行" in result.message
    assert "Microsoft Connect Test" in result.detail
    assert mock_portal.STATE["online"] is True


def test_login_with_wrong_password_fails(mock_net) -> None:  # noqa: ANN001
    mock_portal.STATE["online"] = False
    result = http_login(_account(mock_net[0], mock_net[1], "错误密码"))

    assert result.state == "failed"
    assert mock_portal.STATE["online"] is False


def test_login_with_wrong_username_fails(mock_net) -> None:  # noqa: ANN001
    mock_portal.STATE["online"] = False
    account = _account(mock_net[0], mock_net[1], mock_portal.PASSWORD)
    account.username = "99999999"
    result = http_login(account)

    assert result.state == "failed"


def test_login_against_unreachable_portal_errors() -> None:
    account = Account(
        name="x",
        portal_url="http://127.0.0.1:9/login",
        username="u",
        password="p",
    )
    result = http_login(account, timeout=2)
    assert result.state in ("error", "failed")


def test_login_requires_all_hidden_fields(mock_net) -> None:  # noqa: ANN001
    """模拟门户对隐藏域做强校验：漏一个就拒。

    这是首次端到端跑登录时踩到的真实问题 —— 0MKKey 是空值提交，
    若提交时把它当空值丢掉，门户直接拒。断言能防回退。
    """
    mock_portal.STATE["online"] = False
    result = http_login(_account(mock_net[0], mock_net[1], mock_portal.PASSWORD))
    assert result.state == "success", f"隐藏域未带全：{result.detail}"

    # 反向确认：模拟器确实会因缺隐藏域而拒绝
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        mock_net[0],
        data=b"DDDDD=x&upass=y",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("缺隐藏域时门户不应接受")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
        assert "missing hidden field" in exc.read().decode("utf-8", errors="replace")


def test_empty_hidden_field_is_submitted(mock_net) -> None:  # noqa: ANN001
    """0MKKey 必须以空值出现在请求体里，而不是被当成不存在。"""
    mock_portal.STATE["online"] = False
    result = http_login(_account(mock_net[0], mock_net[1], mock_portal.PASSWORD))
    assert result.state == "success"
    assert mock_portal.STATE["online"] is True


# ---------- Monitor 编排 ----------


def test_monitor_reconnects_on_real_drop(mock_net, store) -> None:  # noqa: ANN001
    """掉线时 Monitor 应自动登录并把状态改回已登录。"""
    from campusnet.monitor import Monitor

    mock_portal.STATE["online"] = False
    account = _account(mock_net[0], mock_net[1], mock_portal.PASSWORD)
    account.max_retries = 2
    acc_id = store.add_account(account)

    outcome = Monitor(store, interval=10).check_account(store.get_account(acc_id))

    assert outcome.online is True
    assert outcome.state == "logged_in"
    assert outcome.attempts == 1
    assert store.get_account(acc_id).last_state == "logged_in"
    assert mock_portal.STATE["online"] is True


def test_monitor_marks_failed_when_portal_refuses(mock_net, store, monkeypatch) -> None:  # noqa: ANN001
    """门户不可达时应重试满次数并标 failed，而不是假装成功。"""
    from campusnet import monitor as monitor_mod
    from campusnet.portal import LoginResult

    mock_portal.STATE["online"] = False
    account = _account(mock_net[0], mock_net[1], mock_portal.PASSWORD)
    account.max_retries = 3
    acc_id = store.add_account(account)

    monkeypatch.setattr(
        monitor_mod.portal, "login", lambda *a, **k: LoginResult("failed", "门户不可达")
    )
    monkeypatch.setattr("time.sleep", lambda *_a: None)

    outcome = Monitor(store, interval=10).check_account(store.get_account(acc_id))

    assert outcome.online is False
    assert outcome.state == "failed"
    assert outcome.attempts == 3
    assert store.get_account(acc_id).last_state == "failed"


def test_monitor_logs_full_drop_sequence(mock_net, store) -> None:  # noqa: ANN001
    """事件日志应完整记录「探活失败 → 登录成功」这条链路。"""
    from campusnet.monitor import Monitor

    mock_portal.STATE["online"] = False
    account = _account(mock_net[0], mock_net[1], mock_portal.PASSWORD)
    account.max_retries = 1
    store.add_account(account)

    Monitor(store, interval=10).check_all()

    events = store.list_events(limit=20)
    stages = [e["stage"] for e in events]
    assert "probe" in stages
    assert "login" in stages

    probe_event = next(e for e in events if e["stage"] == "probe")
    assert probe_event["level"] == "warning"
    assert probe_event["state"] == "offline"

    login_event = next(e for e in events if e["stage"] == "login")
    assert login_event["state"] == "success"


def test_monitor_prunes_events_after_real_run(mock_net, store) -> None:  # noqa: ANN001
    """真实跑一轮后事件表应被裁剪，不会无限增长。"""
    from campusnet.monitor import Monitor

    mock_portal.STATE["online"] = False
    store.add_account(_account(mock_net[0], mock_net[1], mock_portal.PASSWORD))
    store.set_setting("max_events", 5)
    for i in range(30):
        store.add_event(f"噪声 {i}")

    Monitor(store, interval=10).check_all()

    assert store.event_count() <= 6


# ---------- 模拟器自身行为 ----------


def test_mock_probe_redirects_to_portal_when_offline(mock_net) -> None:  # noqa: ANN001
    """掉线时探测地址必须 302 到门户。

    注意：urlopen 默认会自动跟随 302，那样看到的是门户 200，
    断言不到跳转本身。这里必须用不跟随重定向的 opener。
    """
    import urllib.error

    mock_portal.STATE["online"] = False
    opener = _no_redirect_opener()

    try:
        opener.open(mock_net[1], timeout=5)
        raise AssertionError("掉线时探测地址不应返回 2xx")
    except urllib.error.HTTPError as exc:
        assert exc.code == 302
        assert "eportal" in (exc.headers.get("Location") or "")


def test_mock_login_endpoint_rejects_missing_hidden_fields(mock_net) -> None:  # noqa: ANN001
    """缺隐藏域时门户返回 400 —— urllib 对 4xx 会抛异常，要显式捕。"""
    import urllib.error
    import urllib.request

    mock_portal.STATE["online"] = False
    req = urllib.request.Request(
        mock_net[0],
        data=b"DDDDD=20240001&upass=x",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("缺隐藏域时不应被接受")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
        assert "missing hidden field" in exc.read().decode("utf-8", errors="replace")


def test_mock_login_rejects_bad_base64_password(mock_net) -> None:  # noqa: ANN001
    import urllib.error
    import urllib.request

    mock_portal.STATE["online"] = False
    body = b"DDDDD=20240001&upass=notbase64&0MKKey=&user_ip=1.1.1.1&user_mac=AA"
    req = urllib.request.Request(
        mock_net[0],
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert "密码错误" in resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # 密码错误返回 200；若门户改成 4xx 也不应让测试崩掉
        assert "密码错误" in exc.read().decode("utf-8", errors="replace")


def _no_redirect_opener():
    """构建一个不自动跟随 302 的 opener。"""
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, ANN001
            return None

    return urllib.request.build_opener(_NoRedirect())


def test_mock_console_reports_state(mock_net) -> None:  # noqa: ANN001
    import json
    import urllib.request
    from urllib.parse import urlsplit

    parts = urlsplit(mock_net[0])
    admin_state = f"{parts.scheme}://{parts.netloc}/admin/state"

    with urllib.request.urlopen(admin_state, timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    assert "online" in data
    assert data["probe_port"] == 2381
