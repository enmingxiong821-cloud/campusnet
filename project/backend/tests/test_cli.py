"""CLI 命令测试：用户每天实际敲的那几条命令。

之前cli.py 覆盖率 0%，是全项目最低。子命令退出码一旦错，
用户看到的是「检测成功」而实际是失败 —— 这类 bug 靠读代码看不出来。
"""

from __future__ import annotations

import json

import pytest

from campusnet import cli
from campusnet.storage import Account


@pytest.fixture()
def isolated_store(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Account:  # noqa: ANN001
    """让 Store() 指向临时库，避免污染真实数据库。"""
    from campusnet.storage import Store

    db = tmp_path / "cli.db"
    monkeypatch.setenv("CAMPUSNET_DB", str(db))

    store = Store(db)
    account = Account(
        name="模拟账号",
        portal_url="http://127.0.0.1:2270/eportal/portal/login",
        probe_url="http://127.0.0.1:2271/probe",
        username="20240001",
        password="campus@2024",
        password_mode="base64",
        login_mode="http",
    )
    store.add_account(account)
    return account


def run(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    return cli.main(argv)


# ---------- 参数解析 ----------


def test_parser_exposes_all_commands() -> None:
    parser = cli.build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert actions, "应注册子命令"
    choices = actions[0].choices
    assert choices is not None
    assert set(choices) == {
        "serve",
        "add",
        "discover",
        "list",
        "check",
        "events",
        "token",
        "bootstart",
        "doctor",
        "notify",
        "cred",
        "power",
        "console",
        "logs",
        "access",
        "shortcut",
        "setup",
        "menu",
        "profiles",
        "portal-report",
        "openapi",
    }


def test_no_command_exits_nonzero() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code != 0


def test_unknown_command_exits() -> None:
    with pytest.raises(SystemExit):
        cli.main(["teleport"])


def test_add_requires_mandatory_flags() -> None:
    """缺参必须退 2，和 argparse 自身的参数错误一致。

    必须断言 code 而不是只断言 SystemExit：必填校验被从 argparse 层
    下移到 cmd_add（为了让 --show-ssid 不用先凑齐账号密码），这条
    契约就只剩测试守着。写成 SystemExit(1) 测试照样绿，脚本里的
    set -e 却会把「参数写错」当成「执行失败」——两种错误的处置方式
    完全不同，不能混。
    """
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["add", "--name", "x"])
    assert excinfo.value.code == 2


def test_add_show_ssid_needs_no_mandatory_flags() -> None:
    """--show-ssid 是纯诊断开关，不该逼用户先填账号密码。"""
    assert cli.main(["add", "--show-ssid"]) == 0


def test_openapi_out_creates_parent_dir(
    isolated_store,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,  # noqa: ANN001
) -> None:
    """README 里的 `openapi --out doc/openapi.json` 必须能原样复制执行。

    父目录不存在时曾经直接抛 FileNotFoundError。文档给的命令跑不通比命令写错更伤——
    用户会先怀疑自己的环境。仓里其余写盘处都建了父目录，这条守住一致性。
    """
    target = tmp_path / "doc" / "openapi.json"
    assert run(["openapi", "--out", str(target)], monkeypatch) == 0
    spec = json.loads(target.read_text(encoding="utf-8"))
    assert spec["openapi"].startswith("3.1")
    assert spec["paths"]


def test_password_mode_choices_are_enforced() -> None:
    with pytest.raises(SystemExit):
        cli.main(
            [
                "add", "--name", "x", "--portal", "http://a",
                "--username", "u", "--password", "p",
                "--password-mode", "rot13",
            ]
        )


def test_login_mode_choices_are_enforced() -> None:
    with pytest.raises(SystemExit):
        cli.main(
            [
                "add", "--name", "x", "--portal", "http://a",
                "--username", "u", "--password", "p",
                "--login-mode", "telepathy",
            ]
        )


# ---------- add ----------


def test_add_creates_account(isolated_store, capsys: pytest.CaptureFixture) -> None:  # noqa: ANN001
    code = cli.main(
        [
            "add", "--name", "新账号",
            "--portal", "http://10.0.0.1/eportal/portal/login",
            "--username", "20240002",
            "--password", "secret",
            "--password-mode", "base64",
            "--extra", '{"0MKKey":"1"}',
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "已添加账号" in out

    from campusnet.storage import Store

    store = Store()
    names = {a.name for a in store.list_accounts()}
    assert "新账号" in names


def test_add_stores_extra_fields(isolated_store) -> None:  # noqa: ANN001
    cli.main(
        [
            "add", "--name", "带隐藏域",
            "--portal", "http://a/login",
            "--username", "u", "--password", "p",
            "--extra", '{"0MKKey":"1","user_ip":"1.1.1.1"}',
        ]
    )
    from campusnet.storage import Store

    store = Store()
    target = next(a for a in store.list_accounts() if a.name == "带隐藏域")
    assert target.extra_fields == {"0MKKey": "1", "user_ip": "1.1.1.1"}


def test_add_rejects_bad_json(capsys: pytest.CaptureFixture) -> None:
    code = cli.main(
        [
            "add", "--name", "x", "--portal", "http://a",
            "--username", "u", "--password", "p",
            "--extra", "{not json}",
        ]
    )
    assert code == 2
    assert "JSON" in capsys.readouterr().err


def test_add_disabled_account(isolated_store) -> None:  # noqa: ANN001
    cli.main(
        [
            "add", "--name", "停用的",
            "--portal", "http://a/login",
            "--username", "u", "--password", "p",
            "--disabled",
        ]
    )
    from campusnet.storage import Store

    store = Store()
    target = next(a for a in store.list_accounts() if a.name == "停用的")
    assert target.enabled is False


def test_add_records_event(isolated_store) -> None:  # noqa: ANN001
    cli.main(
        ["add", "--name", "记日志", "--portal", "http://a", "--username", "u", "--password", "p"]
    )
    from campusnet.storage import Store

    events = Store().list_events(limit=10)
    assert any("新增账号" in e["message"] for e in events)


# ---------- list ----------


def test_list_shows_account(isolated_store, capsys: pytest.CaptureFixture) -> None:  # noqa: ANN001
    code = cli.main(["list"])
    assert code == 0
    out = capsys.readouterr().out
    assert "模拟账号" in out
    assert "门户地址" in out


def test_list_empty_gives_hint(capsys: pytest.CaptureFixture, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("CAMPUSNET_DB", str(tmp_path / "empty.db"))
    code = cli.main(["list"])
    assert code == 0
    assert "还没有账号" in capsys.readouterr().out


# ---------- check ----------


def test_check_all_online_returns_zero(
    isolated_store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:  # noqa: ANN001
    _patch_online(monkeypatch)
    code = cli.main(["check"])
    assert code == 0
    out = capsys.readouterr().out
    assert "[OK ] 模拟账号" in out


def test_check_all_offline_returns_one(
    isolated_store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:  # noqa: ANN001
    _patch_offline(monkeypatch)
    code = cli.main(["check"])
    assert code == 1
    assert "[FAIL]" in capsys.readouterr().out


def test_check_all_runs_exactly_one_round(
    isolated_store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:  # noqa: ANN001
    """全量 check 只能跑一轮检测。

    这里曾连续调两次 monitor.check_all()：check_all 的锁在 finally 里释放，
    所以第二次不是命中重入保护返回缓存，而是真的再跑一遍探活+复核+登录。
    单账号默认 max_retries=3，于是登录尝试翻成 6 次，事件表重复写入，
    而用户看到的输出还是 1 行——副作用发生了却没留痕迹。

    上面两条用例只断言输出文本，抓不到这个：第二轮结果被覆盖后
    输出长得一模一样。所以必须直接数调用次数。
    """
    from campusnet.monitor import Monitor

    calls: list[int] = []
    original = Monitor.check_all

    def counting(self) -> list:  # noqa: ANN001, ANN202
        calls.append(1)
        return original(self)

    monkeypatch.setattr(Monitor, "check_all", counting)
    _patch_online(monkeypatch)
    assert cli.main(["check"]) == 0
    assert len(calls) == 1, f"check_all 被调 {len(calls)} 次，应为 1"


def test_check_specific_account(isolated_store, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # noqa: ANN001
    _patch_online(monkeypatch)
    code = cli.main(["check", "--account", "1"])
    assert code == 0
    assert "模拟账号" in capsys.readouterr().out


def test_check_missing_account_returns_two(capsys: pytest.CaptureFixture) -> None:
    code = cli.main(["check", "--account", "999"])
    assert code == 2
    assert "不存在" in capsys.readouterr().err


def test_check_no_enabled_accounts(monkeypatch: pytest.MonkeyPatch, tmp_path, capsys) -> None:  # noqa: ANN001
    monkeypatch.setenv("CAMPUSNET_DB", str(tmp_path / "none.db"))
    code = cli.main(["check"])
    assert code == 0
    assert "没有启用中的账号" in capsys.readouterr().out


def test_check_writes_event(isolated_store, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    _patch_online(monkeypatch)
    cli.main(["check", "--account", "1"])
    from campusnet.storage import Store

    events = Store().list_events(limit=20)
    assert any(e["stage"] == "cli" for e in events)


def test_check_headless_flag_propagates(isolated_store, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """--headful 应让 monitor.headless 为 False。"""
    from campusnet.monitor import MonitorManager

    holders: list = []
    original = MonitorManager.ensure

    def spy(self, store):  # noqa: ANN001
        mon = original(self, store)
        holders.append(mon)
        return mon

    monkeypatch.setattr(MonitorManager, "ensure", spy)
    _patch_online(monkeypatch)
    cli.main(["check", "--account", "1", "--headful"])

    # headless 只影响 browser 模式，http 模式的检测结果里看不到，所以断言打在
    # monitor 实例上——cmd_check 拿到 ensure 的返回值之后就地把 headless 改了。
    assert holders, "没有走到 MonitorManager.ensure"
    assert holders[0].headless is False


# ---------- events ----------


def test_events_lists_entries(isolated_store, capsys: pytest.CaptureFixture) -> None:  # noqa: ANN001
    from campusnet.storage import Store

    Store().add_event("测试事件", level="warning", stage="probe")
    code = cli.main(["events", "--limit", "10"])
    assert code == 0
    out = capsys.readouterr().out
    assert "测试事件" in out
    assert "warning" in out


def test_events_empty_is_silent(capsys: pytest.CaptureFixture, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("CAMPUSNET_DB", str(tmp_path / "noev.db"))
    code = cli.main(["events"])
    assert code == 0
    assert capsys.readouterr().out.strip() == ""


def test_events_respects_limit(isolated_store, capsys: pytest.CaptureFixture) -> None:  # noqa: ANN001
    from campusnet.storage import Store

    store = Store()
    for i in range(10):
        store.add_event(f"事件 {i}")
    cli.main(["events", "--limit", "3"])
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 3


# ---------- discover ----------


def test_discover_no_candidate_returns_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        cli.discovery_mod,
        "discover",
        lambda **_k: {
            "probe_status": "redirected",
            "probe_detail": "HTTP 302 -> http://portal",
            "redirect_portal": "http://portal/login",
            "gateways": ["10.0.0.1"],
            "candidates": [],
            "hints": ["未能自动发现门户", "改用浏览器登录"],
        },
    )
    code = cli.main(["discover"])
    assert code == 1
    out = capsys.readouterr().out
    assert "redirected" in out
    assert "http://portal/login" in out
    assert "未能自动发现门户" in out


def test_discover_prints_candidate_and_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        cli.discovery_mod,
        "discover",
        lambda **_k: {
            "probe_status": "redirected",
            "probe_detail": "HTTP 302",
            "redirect_portal": "",
            "gateways": ["10.110.0.1"],
            "candidates": [
                {
                    "vendor": "Dr.COM",
                    "status": 200,
                    "url": "http://10.0.0.1/eportal/portal/login",
                    "title": "Dr.COM 认证系统",
                    "username_field": "DDDDD",
                    "password_field": "upass",
                    "login_path": "/eportal/portal/login/",
                    "extra_fields": {"0MKKey": "", "user_ip": "1.1.1.1"},
                    "needs_captcha": False,
                    "suggested_login_mode": "http",
                    "suggested_password_mode": "base64",
                }
            ],
            "hints": ["最佳候选：Dr.COM"],
        },
    )
    code = cli.main(["discover"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Dr.COM" in out
    assert "DDDDD" in out
    assert "upass" in out
    assert "0MKKey" in out
    assert "一键建号命令" in out
    assert "--password-mode base64" in out
    assert "--login-mode http" in out
    # 隐藏域应给出 --extra 追加提示
    assert "--extra" in out
    assert "0MKKey" in out


def test_discover_flags_captcha(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        cli.discovery_mod,
        "discover",
        lambda **_k: {
            "probe_status": "redirected",
            "probe_detail": "",
            "redirect_portal": "",
            "gateways": [],
            "candidates": [
                {
                    "vendor": "华为",
                    "status": 200,
                    "url": "http://x/portal/login",
                    "title": "",
                    "username_field": "username",
                    "password_field": "password",
                    "login_path": "",
                    "extra_fields": {},
                    "needs_captcha": True,
                    "suggested_login_mode": "browser",
                    "suggested_password_mode": "base64",
                }
            ],
            "hints": [],
        },
    )
    code = cli.main(["discover"])
    assert code == 0
    out = capsys.readouterr().out
    assert "验证码" in out
    assert "浏览器登录" in out
    assert "未识别" not in out  # 字段识别出来了就不该显示未识别


def test_discover_missing_fields_shows_placeholder(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        cli.discovery_mod,
        "discover",
        lambda **_k: {
            "probe_status": "redirected",
            "probe_detail": "",
            "redirect_portal": "",
            "gateways": [],
            "candidates": [
                {
                    "vendor": "未知厂商",
                    "status": 200,
                    "url": "http://x/",
                    "title": "",
                    "username_field": "",
                    "password_field": "",
                    "login_path": "",
                    "extra_fields": {},
                    "needs_captcha": False,
                    "suggested_login_mode": "browser",
                    "suggested_password_mode": "plain",
                }
            ],
            "hints": [],
        },
    )
    cli.main(["discover"])
    out = capsys.readouterr().out
    assert "未识别" in out
    assert "(同页)" in out


def test_discover_no_scan_flag_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    captured = {}

    def fake(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return {
            "probe_status": "online", "probe_detail": "", "redirect_portal": "",
            "gateways": [], "candidates": [], "hints": ["无"],
        }

    monkeypatch.setattr(cli.discovery_mod, "discover", fake)
    cli.main(["discover", "--seed", "http://x/", "--timeout", "2", "--no-scan"])

    assert captured["seed"] == "http://x/"
    assert captured["timeout"] == 2.0
    assert captured["include_gateway_scan"] is False


def test_discover_defaults_scan_enabled(monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    captured = {}

    def fake(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return {
            "probe_status": "online", "probe_detail": "", "redirect_portal": "",
            "gateways": [], "candidates": [], "hints": [],
        }

    monkeypatch.setattr(cli.discovery_mod, "discover", fake)
    cli.main(["discover"])

    assert captured["include_gateway_scan"] is True
    assert captured["seed"] == ""


# ---------- 辅助 ----------


def _patch_online(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 probe 一律返回在线，login 也返回成功。"""
    from campusnet import monitor as monitor_mod
    from campusnet.portal import LoginResult

    monkeypatch.setattr(
        monitor_mod.probe_mod,
        "probe",
        lambda url, timeout=5.0, insecure=False: monitor_mod.probe_mod.ProbeResult(
            True, "online", "ok", url
        ),
    )
    monkeypatch.setattr(
        monitor_mod.portal, "login", lambda *a, **k: LoginResult("success", "登录成功")
    )
    # 不再 patch cli.login：cmd_check --account 已改走 Monitor.check_account，
    # cli 不再是登录的调用方。原先这里 patch 的是一层中转，真实调用点在
    # monitor.portal.login（上面已 patch），这份重复只是让它看起来有两处入口。


def _patch_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """让探活失败、登录也失败，用于验证退出码为 1。"""
    from campusnet import monitor as monitor_mod
    from campusnet.portal import LoginResult

    monkeypatch.setattr(
        monitor_mod.probe_mod,
        "probe",
        lambda url, timeout=5.0, insecure=False: monitor_mod.probe_mod.ProbeResult(
            False, "redirected", "portal", url
        ),
    )
    monkeypatch.setattr(
        monitor_mod.portal, "login", lambda *a, **k: LoginResult("failed", "密码错误")
    )
    monkeypatch.setattr("time.sleep", lambda *_a: None)
