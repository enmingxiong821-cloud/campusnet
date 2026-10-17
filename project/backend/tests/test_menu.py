"""主菜单：覆盖全部能力、分发到各动作、权限不足时提出提权重试。"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from campusnet import bootstart as boot_mod
from campusnet import cli as cli_mod
from campusnet import menu as menu_mod
from campusnet.storage import Account


@pytest.fixture(autouse=True)
def menu_isolated(tmp_db, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN001, ANN201
    """菜单一进来就读库，测试必须换成临时库。

    不换的话读的是用户真实的 `database/*.db`——本机 2268 上确实常驻着一个实例，
    账号数、最近检测时间都会随环境变。端口探测不在这里挡：只有断言到那一行的
    用例自己替换 `service_running`，其余用例走真实探测也无妨（探不通就是 0.5 秒）。
    """
    return tmp_db


def _feed(answers: list[str]):
    remaining = list(answers)

    def ask(_prompt: str = "") -> str:
        return remaining.pop(0) if remaining else "0"

    return ask


def test_menu_lists_every_capability() -> None:
    labels = [item.label for item in menu_mod.ITEMS]

    for keyword in (
        "打开控制台",
        "启动服务",
        "立即检测",
        "添加账号",
        "多端访问",
        "远程接入向导",
        "自检报告",
        "运行日志",
        "电源体检",
        "远程关机",
        "远程重启",
        "注册开机自启",
        "卸载开机自启",
        "失败通知",
        "浏览器登录依赖",
        "快捷方式",
    ):
        assert any(keyword in label for label in labels), keyword


def test_menu_prints_sections_and_exits_on_zero() -> None:
    out: list[str] = []

    code = menu_mod.run_menu(ask=_feed(["0"]), out=out.append)

    text = "\n".join(out)
    assert code == 0
    assert "校园网自连 · 主菜单" in text
    assert "[启动与检测]" in text
    assert "[设置]" in text
    assert "0. 退出" in text


def test_menu_dispatches_choice_to_cli(monkeypatch) -> None:  # noqa: ANN001
    called: list[list[str]] = []
    monkeypatch.setattr(cli_mod, "main", lambda argv: called.append(list(argv)))
    index = menu_mod.index_of("立即检测")
    assert index

    menu_mod.run_menu(ask=_feed([str(index), "", "0"]), out=lambda *_: None)

    assert called == [["check"]]


def test_menu_rejects_invalid_input() -> None:
    out: list[str] = []

    menu_mod.run_menu(ask=_feed(["99", "0"]), out=out.append)

    assert any("输入无效" in line for line in out)


def test_index_of_returns_zero_when_missing() -> None:
    assert menu_mod.index_of("不存在的功能") == 0


def test_register_bootstart_success(monkeypatch) -> None:  # noqa: ANN001
    import getpass

    monkeypatch.setenv("CAMPUSNET_FORCE_PLATFORM", "windows")
    seen: list[dict] = []
    monkeypatch.setattr(
        boot_mod,
        "install",
        lambda **kwargs: seen.append(kwargs)
        or boot_mod.BootStatus(True, True, None, "已注册计划任务"),
    )
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "pw-secret")
    out: list[str] = []

    code = menu_mod.register_bootstart(_feed([]), out.append)

    assert code == 0
    assert any("已开启" in line for line in out)
    # 密码必须走 getpass（不回显）并且原样交给任务计划程序保存。
    assert seen == [{"port": 2268, "mode": "task", "password": "pw-secret"}]


def test_register_bootstart_uses_default_carrier_off_windows(
    monkeypatch,  # noqa: ANN001
) -> None:
    """Linux / macOS 上没有「计划任务」这条路。

    原来这里不分平台一律 mode="task" 并且先要一次 Windows 账户密码，两个平台上
    必然失败在「计划任务模式目前只支持 Windows」——菜单里这一项从来没成功过。
    """
    import getpass

    monkeypatch.setenv("CAMPUSNET_FORCE_PLATFORM", "linux")
    seen: list[dict] = []
    monkeypatch.setattr(
        boot_mod,
        "install",
        lambda **kwargs: seen.append(kwargs)
        or boot_mod.BootStatus(True, True, None, "已注册开机自启动"),
    )
    asked: list[str] = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": asked.append(prompt) or "x")
    out: list[str] = []

    assert menu_mod.register_bootstart(_feed([]), out.append) == 0

    assert seen == [{"port": 2268, "mode": "auto", "password": ""}]
    assert asked == []  # 不该对着 Linux 用户问 Windows 账户密码


def test_register_bootstart_explains_login_only_carrier(
    monkeypatch,  # noqa: ANN001
) -> None:
    """装上了不等于「有电就连接」：详情里那句「登录后才会拉起」必须转达给用户。"""
    import getpass

    monkeypatch.setenv("CAMPUSNET_FORCE_PLATFORM", "windows")
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "pw")
    monkeypatch.setattr(
        boot_mod,
        "install",
        lambda **kwargs: boot_mod.BootStatus(
            True, True, None, "已注册开机自启动（登录后才会拉起；要「有电就连接」…）"
        ),
    )
    out: list[str] = []

    assert menu_mod.register_bootstart(_feed([]), out.append) == 0

    assert any("登录后才会拉起" in line for line in out)


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("windows", "无需登录"),
        ("linux", "系统级单元"),
        ("darwin", "系统级单元"),
    ],
)
def test_bootstart_hint_matches_what_it_actually_installs(
    monkeypatch,  # noqa: ANN001
    platform: str,
    expected: str,
) -> None:
    """菜单上那句承诺得与实际装的载体一致。

    「无需登录」只有 Windows 的计划任务做得到；另外两个平台装的是用户级载体。
    副标题照抄 Windows 那句，用户就会拿着一个做不到的说法去注册。
    """
    monkeypatch.setenv("CAMPUSNET_FORCE_PLATFORM", platform)

    hint = menu_mod.bootstart_hint()

    assert expected in hint
    if platform == "windows":
        assert "系统级单元" not in hint
    else:
        assert "需要管理员" not in hint


def test_register_bootstart_offers_elevation(monkeypatch) -> None:  # noqa: ANN001
    import getpass

    monkeypatch.setattr(
        boot_mod,
        "install",
        lambda **kwargs: boot_mod.BootStatus(
            True, False, None, "需要管理员权限才能注册「开机触发、无需登录」的任务", False
        ),
    )
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "pw")
    relaunched: list[list[str]] = []
    monkeypatch.setattr(
        menu_mod, "_relaunch_elevated", lambda args: relaunched.append(list(args)) or 0
    )
    out: list[str] = []

    code = menu_mod.register_bootstart(_feed(["y"]), out.append)

    assert code == 0
    assert relaunched == [["bootstart", "install", "--mode", "task"]]
    assert any("管理员权限" in line for line in out)


def test_register_bootstart_declined_elevation(monkeypatch) -> None:  # noqa: ANN001
    import getpass

    monkeypatch.setattr(
        boot_mod,
        "install",
        lambda **kwargs: boot_mod.BootStatus(True, False, None, "需要管理员权限才能注册", False),
    )
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "pw")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        menu_mod, "_relaunch_elevated", lambda args: calls.append(list(args)) or 0
    )

    code = menu_mod.register_bootstart(_feed(["n"]), lambda *_: None)

    assert code == 1
    assert calls == []


def test_register_bootstart_passes_paths_api_port(monkeypatch) -> None:  # noqa: ANN001
    """自启动项的端口必须取自 paths.API_PORT，不能写死 2268。

    回归防护：端口写死时，改了端口（CAMPUSNET_API_PORT）的用户会被注册出一份
    指向 2268 的自启动项，开机后连的不是自己拉起的服务。这条把 API_PORT 改成
    非默认值，断言真的被透传下去。
    """
    from campusnet import paths

    monkeypatch.setenv("CAMPUSNET_FORCE_PLATFORM", "linux")
    monkeypatch.setattr(paths, "API_PORT", 12345)
    seen: list[dict] = []
    monkeypatch.setattr(
        boot_mod,
        "install",
        lambda **kwargs: seen.append(kwargs)
        or boot_mod.BootStatus(True, True, None, "已注册开机自启动"),
    )
    out: list[str] = []

    assert menu_mod.register_bootstart(_feed([]), out.append) == 0

    assert seen == [{"port": 12345, "mode": "auto", "password": ""}]


def test_configure_notify_saves_and_tests(monkeypatch, store) -> None:  # noqa: ANN001
    from campusnet import notify as notify_mod

    sent: list[str] = []
    monkeypatch.setattr(
        notify_mod, "send_test", lambda notifier: sent.append(notifier.url) or True
    )
    out: list[str] = []

    code = menu_mod.configure_notify(_feed(["http://hook.test/x"]), out.append)

    assert code == 0
    assert store.get_setting("notify_url") == "http://hook.test/x"
    assert sent == ["http://hook.test/x"]
    assert any("测试通知已发送" in line for line in out)


def test_configure_notify_cancel(store) -> None:  # noqa: ANN001
    code = menu_mod.configure_notify(_feed([""]), lambda *_: None)

    assert code == 0
    assert store.get_setting("notify_url") is None


def test_install_browser_skips_when_ready(monkeypatch) -> None:  # noqa: ANN001
    from campusnet import setup_flow

    monkeypatch.setattr(setup_flow, "browser_ready", lambda: True)
    out: list[str] = []

    assert menu_mod.install_browser(_feed([]), out.append) == 0
    assert any("已安装" in line for line in out)


def test_install_browser_declined(monkeypatch) -> None:  # noqa: ANN001
    from campusnet import setup_flow

    called: list[int] = []
    monkeypatch.setattr(setup_flow, "browser_ready", lambda: False)
    monkeypatch.setattr(
        setup_flow, "install_browser_deps", lambda: called.append(1) or (True, "ok")
    )

    assert menu_mod.install_browser(_feed(["n"]), lambda *_: None) == 0
    assert called == []


def test_guided_add_creates_account(monkeypatch, store) -> None:  # noqa: ANN001
    import getpass

    from campusnet import discovery as discovery_mod

    monkeypatch.setattr(
        discovery_mod,
        "discover",
        lambda **kwargs: {
            "candidates": [
                {
                    "vendor": "Dr.COM",
                    "url": "http://10.1.2.3/eportal/portal/login",
                    "username_field": "DDDDD",
                    "password_field": "upass",
                    "suggested_login_mode": "http",
                    "suggested_password_mode": "base64",
                    "extra_fields": {"0MKKey": "x"},
                }
            ]
        },
    )
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "secret")
    out: list[str] = []

    code = menu_mod.guided_add(_feed(["宿舍", "20240001"]), out.append)

    assert code == 0
    accounts = store.list_accounts()
    assert len(accounts) == 1
    account = accounts[0]
    assert account.name == "宿舍"
    assert account.username == "20240001"
    assert account.password == "secret"
    assert account.username_field == "DDDDD"
    assert account.password_mode == "base64"
    assert account.extra_fields == {"0MKKey": "x"}
    assert any("已添加账号" in line for line in out)


def test_guided_add_without_candidates(monkeypatch) -> None:  # noqa: ANN001
    from campusnet import discovery as discovery_mod

    monkeypatch.setattr(discovery_mod, "discover", lambda **kwargs: {"candidates": []})
    out: list[str] = []

    assert menu_mod.guided_add(_feed([]), out.append) == 1
    assert any("没发现可用门户" in line for line in out)


# ---------------------------------------------- 中断与失败分支（F2）


def test_relaunch_elevated_without_powershell(monkeypatch) -> None:  # noqa: ANN001
    """没有 PowerShell 时返回失败码，不能让用户看到 traceback。"""
    monkeypatch.setattr(menu_mod.shutil, "which", lambda _name: None)

    assert menu_mod._relaunch_elevated(["bootstart", "install"]) == 1


def test_relaunch_elevated_runs_and_reports(monkeypatch) -> None:  # noqa: ANN001
    calls: list[list[str]] = []
    monkeypatch.setattr(menu_mod.shutil, "which", lambda _name: "powershell")
    monkeypatch.setattr(
        menu_mod.subprocess,
        "run",
        lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0),
    )

    assert menu_mod._relaunch_elevated(["power", "off"]) == 0
    assert calls and "RunAs" in " ".join(calls[0])
    # 不带 /c 时 cmd.exe 只开一个空窗口、命令根本不执行
    assert "/c " in " ".join(calls[0])


def test_relaunch_elevated_reports_child_failure(monkeypatch) -> None:  # noqa: ANN001
    """提权成功但子命令失败时不能再报成功。"""
    monkeypatch.setattr(menu_mod.shutil, "which", lambda _name: "powershell")
    monkeypatch.setattr(
        menu_mod.subprocess,
        "run",
        lambda args, **kwargs: SimpleNamespace(returncode=1),
    )

    assert menu_mod._relaunch_elevated(["bootstart", "install"]) == 1


def test_relaunch_elevated_swallows_oserror(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(menu_mod.shutil, "which", lambda _name: "powershell")

    def boom(*args, **kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(menu_mod.subprocess, "run", boom)

    assert menu_mod._relaunch_elevated(["power", "off"]) == 1


def test_register_bootstart_aborts_on_interrupt(monkeypatch) -> None:  # noqa: ANN001
    """Ctrl+C / 输入流结束等同于取消，返回 2 而不是抛出去。

    必须注入 Windows：只有计划任务那条路才会去读密码，打断点才有意义。
    不注入的话 Linux 上平台名是 linux、`mode="auto"`，压根不走 getpass——
    会真的往 `~/.config/systemd/user/` 写单元文件，然后返回 0。
    同理 `install` 也替掉，免得哪天分支改回去时用例变成往盘上写东西的副作用。
    """
    import getpass

    monkeypatch.setenv("CAMPUSNET_FORCE_PLATFORM", "windows")
    installed: list[dict] = []
    monkeypatch.setattr(boot_mod, "install", lambda **kwargs: installed.append(kwargs))

    def abort(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr(getpass, "getpass", abort)

    assert menu_mod.register_bootstart(_feed([]), lambda *_: None) == 2
    assert installed == []  # 密码都没拿到手，绝不能走到安装


# ---------------------------------------------- 关机 / 重启前的确认


def test_power_handler_cancels_on_declined_confirm(monkeypatch) -> None:  # noqa: ANN001
    """默认答案是否：直接回车不能关机。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(cli_mod, "main", lambda argv: calls.append(list(argv)))
    out: list[str] = []

    code = menu_mod._power_handler("off", "关机")(_feed([""]), out.append)

    assert code == 0
    assert calls == []
    assert any("已取消" in line for line in out)


def test_power_handler_accepts_yes_variants(monkeypatch) -> None:  # noqa: ANN001
    calls: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(cli_mod, "main", fake_main)

    assert menu_mod._power_handler("off", "关机")(_feed(["y"]), lambda *_: None) == 0
    assert menu_mod._power_handler("reboot", "重启")(_feed(["YES"]), lambda *_: None) == 0

    assert calls == [["power", "off"], ["power", "reboot"]]


def test_power_handler_warns_about_hard_to_reverse_shutdown() -> None:
    """关机不可逆，提示里必须点明要靠 WOL / 插座才能再开机。"""
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return "n"

    menu_mod._power_handler("off", "关机")(ask, lambda *_: None)
    menu_mod._power_handler("reboot", "重启")(ask, lambda *_: None)

    assert "WOL" in prompts[0]
    # 重启能自己回来，不需要吓唬用户。
    assert "WOL" not in prompts[1]


def test_power_handler_cancels_on_interrupt(monkeypatch) -> None:  # noqa: ANN001
    calls: list[list[str]] = []
    monkeypatch.setattr(cli_mod, "main", lambda argv: calls.append(list(argv)))

    def ask(_prompt: str) -> str:
        raise KeyboardInterrupt

    out: list[str] = []

    assert menu_mod._power_handler("off", "关机")(ask, out.append) == 0
    assert calls == []
    assert any("已取消" in line for line in out)


def test_guided_add_reports_discovery_failure(monkeypatch) -> None:  # noqa: ANN001
    from campusnet import discovery as discovery_mod

    def boom(**kwargs):
        raise RuntimeError("扫描失败")

    monkeypatch.setattr(discovery_mod, "discover", boom)
    out: list[str] = []

    assert menu_mod.guided_add(_feed([]), out.append) == 1
    assert any("发现问题失败" in line and "扫描失败" in line for line in out)


def _fake_discovery(monkeypatch, candidate: dict) -> None:  # noqa: ANN001
    from campusnet import discovery as discovery_mod

    monkeypatch.setattr(
        discovery_mod, "discover", lambda **kwargs: {"candidates": [candidate]}
    )
    monkeypatch.setattr(
        discovery_mod,
        "to_account_fields",
        lambda item: {"login_mode": "http"} if isinstance(item, dict) else {},
    )


def test_guided_add_cancels_without_username(monkeypatch, store) -> None:  # noqa: ANN001
    _fake_discovery(monkeypatch, {"vendor": "drcom", "url": "http://10.0.0.1/login"})
    out: list[str] = []

    # 备注名用默认、账号留空
    assert menu_mod.guided_add(_feed(["", ""]), out.append) == 2
    assert any("没填账号" in line for line in out)


def test_guided_add_cancels_without_password(monkeypatch, store) -> None:  # noqa: ANN001
    import getpass

    _fake_discovery(monkeypatch, {"vendor": "srun", "url": "http://10.0.0.1/login"})
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: "")
    out: list[str] = []

    assert menu_mod.guided_add(_feed(["", "20240001"]), out.append) == 2
    assert any("没填密码" in line for line in out)


def test_guided_add_accepts_object_candidate(monkeypatch, store) -> None:  # noqa: ANN001
    """候选既可能是 dict，也可能是带 as_dict() 的对象。"""
    import getpass

    class Candidate:
        def as_dict(self) -> dict:
            return {"vendor": "h3c", "url": "http://10.0.0.1/login"}

    _fake_discovery(monkeypatch, Candidate())  # type: ignore[arg-type]
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: "pw")

    assert menu_mod.guided_add(_feed(["宿舍", "20240001"]), lambda *_: None) == 0


def test_install_browser_runs_installer(monkeypatch) -> None:  # noqa: ANN001
    from campusnet import setup_flow

    monkeypatch.setattr(setup_flow, "browser_ready", lambda: False)
    monkeypatch.setattr(
        setup_flow, "install_browser_deps", lambda: (True, "装好了")
    )
    out: list[str] = []

    assert menu_mod.install_browser(_feed(["y"]), out.append) == 0
    assert any("装好了" in line for line in out)


def test_install_browser_reports_failure(monkeypatch) -> None:  # noqa: ANN001
    from campusnet import setup_flow

    monkeypatch.setattr(setup_flow, "browser_ready", lambda: False)
    monkeypatch.setattr(
        setup_flow, "install_browser_deps", lambda: (False, "下载失败")
    )
    out: list[str] = []

    assert menu_mod.install_browser(_feed(["y"]), out.append) == 1
    assert any("下载失败" in line for line in out)


def test_run_menu_returns_zero_on_interrupt() -> None:  # noqa: ANN001
    """菜单提示处按 Ctrl+C / 输入结束，应干净退出而不是抛栈。"""

    def ask(_prompt: str = "") -> str:
        raise KeyboardInterrupt

    assert menu_mod.run_menu(ask, lambda *_: None) == 0


def test_run_menu_dispatches_handler(monkeypatch) -> None:  # noqa: ANN001
    """带 handler 的条目走 handler，不经过 CLI。"""
    calls: list[int] = []

    class FakeItem:
        label = "假条目"
        hint = ""
        section = ""
        args = ()
        handler = staticmethod(lambda ask, out: calls.append(1) or 0)

    monkeypatch.setattr(menu_mod, "ITEMS", (FakeItem(),))

    assert menu_mod.run_menu(_feed(["1", "0"]), lambda *_: None) == 0
    assert calls == [1]


# ---------- 动作返回码 ----------


def test_run_menu_survives_action_systemexit(monkeypatch, tmp_db) -> None:  # noqa: ANN001
    """动作抛 SystemExit 时菜单要活下来。

    `serve` 发现已有实例在跑、或端口被占时会 `raise SystemExit(1)`；桌面快捷方式
    双击进来的是一个 cmd 窗口，让它穿出去就等于窗口一闪而过，用户看不到那句错误。
    """
    monkeypatch.setattr(
        cli_mod, "main", lambda argv: (_ for _ in ()).throw(SystemExit(1))
    )

    out: list[str] = []
    index = menu_mod.index_of("启动服务")
    code = menu_mod.run_menu(_feed([str(index), "", "0"]), out.append)

    assert code == 0  # 菜单本身正常退出，不是被 SystemExit 带跑
    assert any("退出码 1" in line for line in out)


def test_run_menu_reports_failed_action(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(cli_mod, "main", lambda argv: 1)

    out: list[str] = []
    index = menu_mod.index_of("立即检测")
    menu_mod.run_menu(_feed([str(index), "", "0"]), out.append)

    assert any("退出码 1" in line for line in out)


def test_run_menu_keeps_quiet_on_cancel(monkeypatch) -> None:  # noqa: ANN001
    """退出码 2 在本项目里是「用户取消」，不该被说成失败。"""
    monkeypatch.setattr(cli_mod, "main", lambda argv: 2)

    out: list[str] = []
    index = menu_mod.index_of("立即检测")
    menu_mod.run_menu(_feed([str(index), "", "0"]), out.append)

    assert not any("退出码" in line for line in out)


def test_run_menu_prints_status_summary(monkeypatch, store) -> None:  # noqa: ANN001
    store.add_account(Account(name="宿舍", username="2024", password="p"))
    monkeypatch.setattr(menu_mod, "service_running", lambda *a, **kw: False)
    out: list[str] = []

    menu_mod.run_menu(_feed(["0"]), out.append)

    assert out[0] == "校园网自连 · 主菜单"
    assert "账号 1 个（启用 1）" in out[1]
    assert "服务未启动" in out[1]


# ---------- 状态行 ----------


def test_status_line_without_accounts(store) -> None:
    line = menu_mod.status_line(store, running=lambda: False)

    assert "还没有账号" in line
    assert "服务未启动" in line


def test_status_line_counts_enabled_and_checked() -> None:
    """`add_account` 不落 last_checked_at（那是每次检测才写的），所以这里用假 store
    直接喂一个时间戳——要验的正是「取出最新那个再折算成多久前」。"""

    class FakeStore:
        def list_accounts(self):
            return [
                Account(
                    name="宿舍",
                    username="1",
                    password="p",
                    last_checked_at="2026-10-14 12:00:00",
                ),
                Account(name="实验室", username="2", password="p", enabled=False),
            ]

    line = menu_mod.status_line(
        FakeStore(), running=lambda: True, now=datetime(2026, 10, 14, 12, 7, 0)
    )

    assert "账号 2 个（启用 1）" in line
    assert "最近检测 7 分钟前" in line
    assert "服务运行中" in line


def test_status_line_uses_newest_check() -> None:
    class FakeStore:
        def list_accounts(self):
            return [
                Account(last_checked_at="2026-10-14 12:00:00"),
                Account(last_checked_at="2026-10-14 11:30:00"),
            ]

    line = menu_mod.status_line(
        FakeStore(), running=lambda: True, now=datetime(2026, 10, 14, 12, 5, 0)
    )

    assert "最近检测 5 分钟前" in line


def test_status_line_swallows_store_failure(monkeypatch) -> None:  # noqa: ANN001
    """读库炸了也要给出一行，菜单不能因此起不来。"""
    import campusnet.storage as storage_mod

    monkeypatch.setattr(
        storage_mod, "Store", lambda *a, **kw: (_ for _ in ()).throw(OSError("db 坏了"))
    )

    line = menu_mod.status_line(None, running=lambda: False)

    assert line == "服务未启动（选「启动服务」）"


@pytest.mark.parametrize(
    ("seconds_ago", "expected"),
    [
        (0, "刚刚"),
        (59, "刚刚"),
        (60, "1 分钟前"),
        (3599, "59 分钟前"),
        (3600, "1 小时前"),
        (86399, "23 小时前"),
        (86400, "1 天前"),
        (-120, "刚刚"),  # 时钟回拨出负数，不能显示「-2 分钟前」
    ],
)
def test_ago_buckets(seconds_ago: int, expected: str) -> None:
    import datetime as dt

    now = dt.datetime(2026, 10, 14, 12, 0, 0)
    stamp = (now + dt.timedelta(seconds=-seconds_ago)).strftime("%Y-%m-%d %H:%M:%S")

    assert menu_mod._ago(stamp, now) == expected


def test_ago_handles_missing_and_bad_values() -> None:
    assert menu_mod._ago(None) == ""
    assert menu_mod._ago("不是时间") == ""


def test_service_running_false_when_nothing_listens() -> None:
    """给一个几乎不可能有人监听的端口，探不通要返回 False 而不是抛。"""
    assert menu_mod.service_running(port=9, timeout=0.2) is False


def test_run_menu_returns_zero_when_return_prompt_interrupted(
    monkeypatch,
) -> None:  # noqa: ANN001
    """执行完动作、等「按回车返回」时输入结束，也要干净退出。"""

    class FakeItem:
        label = "假条目"
        hint = ""
        section = ""
        args = ()
        handler = staticmethod(lambda ask, out: 0)

    monkeypatch.setattr(menu_mod, "ITEMS", (FakeItem(),))
    answers = iter(["1"])

    def ask(_prompt: str = "") -> str:
        try:
            return next(answers)
        except StopIteration:
            raise KeyboardInterrupt from None

    assert menu_mod.run_menu(ask, lambda *_: None) == 0
