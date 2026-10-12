"""主菜单：覆盖全部能力、分发到各动作、权限不足时提出提权重试。"""

from __future__ import annotations

from campusnet import bootstart as boot_mod
from campusnet import cli as cli_mod
from campusnet import menu as menu_mod


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
        "自检报告",
        "运行日志",
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
    monkeypatch.setattr(
        boot_mod,
        "install",
        lambda **kwargs: boot_mod.BootStatus(True, True, None, "已注册计划任务"),
    )
    out: list[str] = []

    code = menu_mod.register_bootstart(_feed(["pw"]), out.append)

    assert code == 0
    assert any("已开启" in line for line in out)


def test_register_bootstart_offers_elevation(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        boot_mod,
        "install",
        lambda **kwargs: boot_mod.BootStatus(
            True, False, None, "需要管理员权限才能注册「开机触发、无需登录」的任务", False
        ),
    )
    relaunched: list[list[str]] = []
    monkeypatch.setattr(
        menu_mod, "_relaunch_elevated", lambda args: relaunched.append(list(args)) or 0
    )
    out: list[str] = []

    code = menu_mod.register_bootstart(_feed(["pw", "y"]), out.append)

    assert code == 0
    assert relaunched == [["bootstart", "install", "--mode", "task"]]
    assert any("管理员权限" in line for line in out)


def test_register_bootstart_declined_elevation(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        boot_mod,
        "install",
        lambda **kwargs: boot_mod.BootStatus(True, False, None, "需要管理员权限才能注册", False),
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        menu_mod, "_relaunch_elevated", lambda args: calls.append(list(args)) or 0
    )

    code = menu_mod.register_bootstart(_feed(["pw", "n"]), lambda *_: None)

    assert code == 1
    assert calls == []


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
