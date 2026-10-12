"""主菜单：分发到各子命令，并在权限不足时提出提权重试。"""

from __future__ import annotations

from campusnet import bootstart as boot_mod
from campusnet import cli as cli_mod
from campusnet import menu as menu_mod


def _feed(answers: list[str]):
    remaining = list(answers)

    def ask(_prompt: str = "") -> str:
        return remaining.pop(0) if remaining else "0"

    return ask


def test_menu_prints_items_and_exits_on_zero() -> None:
    out: list[str] = []

    code = menu_mod.run_menu(ask=_feed(["0"]), out=out.append)

    text = "\n".join(out)
    assert code == 0
    assert "校园网自连 · 主菜单" in text
    assert "打开控制台" in text
    assert "注册开机自启" in text
    assert "0. 退出" in text


def test_menu_dispatches_choice_to_cli(monkeypatch) -> None:
    called: list[list[str]] = []
    monkeypatch.setattr(cli_mod, "main", lambda argv: called.append(list(argv)))
    out: list[str] = []

    menu_mod.run_menu(ask=_feed(["3", "", "0"]), out=out.append)

    assert called == [["check"]]


def test_menu_rejects_invalid_input() -> None:
    out: list[str] = []

    menu_mod.run_menu(ask=_feed(["99", "0"]), out=out.append)

    assert any("输入无效" in line for line in out)


def test_register_bootstart_success(monkeypatch) -> None:
    monkeypatch.setattr(
        boot_mod,
        "install",
        lambda **kwargs: boot_mod.BootStatus(True, True, None, "已注册计划任务"),
    )
    out: list[str] = []

    code = menu_mod.register_bootstart(_feed(["pw"]), out.append)

    assert code == 0
    assert any("已开启" in line for line in out)


def test_register_bootstart_offers_elevation(monkeypatch) -> None:
    """非管理员时先解释原因，再问是否提权重试。"""
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


def test_register_bootstart_declined_elevation(monkeypatch) -> None:
    monkeypatch.setattr(
        boot_mod,
        "install",
        lambda **kwargs: boot_mod.BootStatus(
            True, False, None, "需要管理员权限才能注册", False
        ),
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        menu_mod, "_relaunch_elevated", lambda args: calls.append(list(args)) or 0
    )
    out: list[str] = []

    code = menu_mod.register_bootstart(_feed(["pw", "n"]), out.append)

    assert code == 1
    assert calls == []
