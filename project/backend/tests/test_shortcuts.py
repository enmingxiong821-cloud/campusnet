"""快捷方式生成与多端访问信息。"""

from __future__ import annotations

import os
import webbrowser
from pathlib import Path

import pytest

from campusnet import access as access_mod
from campusnet import cli as cli_mod
from campusnet import shortcuts as shortcuts_mod


# ---------- 快捷方式 ----------


def test_desktop_has_exactly_one_shortcut() -> None:
    """桌面只留一个入口：其余操作都在菜单里选。"""
    assert len(shortcuts_mod.DEFAULT_SHORTCUTS) == 1
    entry = shortcuts_mod.DEFAULT_SHORTCUTS[0]
    assert entry.name == "校园网自连"
    assert entry.args == ("menu",)


def test_legacy_names_cover_old_shortcuts() -> None:
    """旧版本生成过的名字要留下来，才能在重新生成时清掉。"""
    for legacy in ("启动校园网服务", "打开控制台", "远程关机"):
        assert legacy in shortcuts_mod.LEGACY_NAMES


def test_create_linux_writes_desktop_entries(tmp_path: Path) -> None:
    ok, message = shortcuts_mod.create(tmp_path, platform="linux")

    assert ok is True
    assert "快捷方式" in message
    files = sorted(item.name for item in tmp_path.iterdir())
    assert len(files) == len(shortcuts_mod.DEFAULT_SHORTCUTS)
    entry = tmp_path / "校园网自连.desktop"
    text = entry.read_text(encoding="utf-8")
    assert "Type=Application" in text
    assert "menu" in text
    assert "Terminal=true" in text
    if os.name == "posix":
        assert entry.stat().st_mode & 0o111


def test_create_macos_writes_command_scripts(tmp_path: Path) -> None:
    ok, _ = shortcuts_mod.create(tmp_path, platform="darwin")

    assert ok is True
    script = (tmp_path / "校园网自连.command").read_text(encoding="utf-8")
    assert script.startswith("#!/bin/sh")
    assert "menu" in script


def test_create_windows_calls_powershell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scripts: list[str] = []
    monkeypatch.setattr(
        shortcuts_mod,
        "_run_powershell",
        lambda script: (scripts.append(script) or (True, "")),
    )

    ok, message = shortcuts_mod.create(tmp_path, platform="windows")

    assert ok is True
    assert "快捷方式" in message
    assert len(scripts) == 1
    assert "WScript.Shell" in scripts[0]
    for item in shortcuts_mod.DEFAULT_SHORTCUTS:
        assert item.name + ".lnk" in scripts[0]
    # 交互式命令要包一层 cmd /k，否则双击后窗口一闪而过看不到报错
    assert "/k " in scripts[0]


def test_create_cleans_up_legacy_shortcuts(tmp_path: Path) -> None:
    """桌面上的旧快捷方式要在重新生成时被清掉，不能越攒越多。"""
    for legacy in ("启动校园网服务", "远程关机"):
        (tmp_path / f"{legacy}.desktop").write_text("old", encoding="utf-8")

    ok, message = shortcuts_mod.create(tmp_path, platform="linux")

    assert ok is True
    assert "清理了 2 个旧快捷方式" in message
    assert not (tmp_path / "启动校园网服务.desktop").exists()
    assert not (tmp_path / "远程关机.desktop").exists()


def test_create_rejects_unknown_platform(tmp_path: Path) -> None:
    ok, message = shortcuts_mod.create(tmp_path, platform="plan9")

    assert ok is False
    assert "plan9" in message


def test_cli_shortcut_prints_result(tmp_path: Path, capsys) -> None:  # noqa: ANN001
    code = cli_mod.main(["shortcut", "--dir", str(tmp_path), "--platform", "linux"])

    assert code == 0
    assert "快捷方式" in capsys.readouterr().out


def test_cli_shortcut_reports_failure(capsys) -> None:  # noqa: ANN001
    code = cli_mod.main(["shortcut", "--platform", "plan9"])

    assert code == 1
    assert "失败" in capsys.readouterr().err


# ---------- 多端访问 ----------


def test_access_report_lists_addresses_and_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(access_mod, "lan_ip", lambda: "192.168.1.23")
    monkeypatch.setattr(access_mod, "tailscale_ip", lambda: "100.64.0.9")
    monkeypatch.setattr(access_mod, "token_text", lambda: "TESTTOKEN")

    text = "\n".join(access_mod.report(host="0.0.0.0", port=2268))

    assert "http://127.0.0.1:2268" in text
    assert "http://192.168.1.23:2268" in text
    assert "http://100.64.0.9:2268" in text
    assert "TESTTOKEN" in text


def test_access_report_warns_when_loopback_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(access_mod, "lan_ip", lambda: "")
    monkeypatch.setattr(access_mod, "tailscale_ip", lambda: "")
    monkeypatch.setattr(access_mod, "token_text", lambda: "T")

    text = "\n".join(access_mod.report(host="127.0.0.1", port=2268))

    assert "只监听本机" in text


def test_cli_access_prints_report(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:  # noqa: ANN001
    monkeypatch.setattr(access_mod, "lan_ip", lambda: "")
    monkeypatch.setattr(access_mod, "tailscale_ip", lambda: "")
    monkeypatch.setattr(access_mod, "token_text", lambda: "T")

    assert cli_mod.main(["access"]) == 0

    assert "控制台令牌" in capsys.readouterr().out


# ---------- console / logs ----------


def test_cli_console_opens_browser(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:  # noqa: ANN001
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    assert cli_mod.main(["console", "--port", "2268"]) == 0

    assert opened == ["http://127.0.0.1:2268"]
    assert "已在浏览器打开" in capsys.readouterr().out


def test_cli_logs_prints_tail(capsys) -> None:  # noqa: ANN001
    # conftest 的 autouse 夹具已把 CAMPUSNET_LOG_DIR 指到临时目录
    from campusnet import logging_setup

    target = logging_setup.log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("第一行\n第二行\n", encoding="utf-8")

    assert cli_mod.main(["logs", "--lines", "1"]) == 0

    out = capsys.readouterr().out
    assert str(target) in out
    assert "第二行" in out
    assert "第一行" not in out


def test_cli_logs_without_file(capsys) -> None:  # noqa: ANN001
    assert cli_mod.main(["logs"]) == 0

    assert "还没有日志文件" in capsys.readouterr().out
