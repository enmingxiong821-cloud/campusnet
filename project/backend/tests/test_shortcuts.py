"""快捷方式生成与多端访问信息。"""

from __future__ import annotations

import json
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


# ---------- 平台与失败分支（F2）----------


def test_desktop_dir_uses_userprofile_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert shortcuts_mod.desktop_dir(platform="windows") == tmp_path / "Desktop"


def test_desktop_dir_falls_back_to_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """USERPROFILE 没设（或被清空）时回落到 home，不能拼出相对路径。"""
    monkeypatch.setenv("USERPROFILE", "")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    fallback = shortcuts_mod.desktop_dir(platform="windows")

    assert fallback.name == "Desktop"
    assert fallback.is_absolute()


def test_xdg_desktop_dir_reads_user_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """中文 Linux 的桌面目录叫「桌面」，写死 Desktop 会落到看不见的目录。"""
    config = tmp_path / "config"
    config.mkdir()
    (config / "user-dirs.dirs").write_text(
        'XDG_DESKTOP_DIR="$HOME/桌面"\n', encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    assert shortcuts_mod._xdg_desktop() == Path.home() / "桌面"


def test_xdg_desktop_dir_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "user-dirs.dirs").write_text('XDG_DESKTOP_DIR="$HOME/"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    assert shortcuts_mod._xdg_desktop() is None


def test_remove_legacy_skips_unremovable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """删不掉的旧快捷方式只是跳过，不能让整次生成失败。"""
    target = tmp_path / "启动校园网服务.lnk"
    target.write_text("old", encoding="utf-8")

    def boom(self, *args, **kwargs):
        raise OSError("文件被占用")

    monkeypatch.setattr(Path, "unlink", boom)

    assert shortcuts_mod._remove_legacy(tmp_path) == []


def test_ps_quote_escapes_single_quotes() -> None:
    assert shortcuts_mod._ps_quote("it's") == "'it''s'"


def test_run_powershell_without_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shortcuts_mod.shutil, "which", lambda _name: None)

    ok, message = shortcuts_mod._run_powershell("echo 1")

    assert ok is False
    assert "找不到 powershell" in message


def test_run_powershell_reports_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shortcuts_mod.shutil, "which", lambda _name: "powershell")

    def boom(*args, **kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(shortcuts_mod.subprocess, "run", boom)

    ok, message = shortcuts_mod._run_powershell("echo 1")

    assert ok is False
    assert "OSError" in message


def test_run_powershell_reports_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shortcuts_mod.shutil, "which", lambda _name: "powershell")
    monkeypatch.setattr(
        shortcuts_mod.subprocess,
        "run",
        lambda *a, **k: _proc(1, "拒绝访问"),
    )

    ok, message = shortcuts_mod._run_powershell("echo 1")

    assert ok is False
    assert "拒绝访问" in message


def test_run_powershell_reports_empty_output_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shortcuts_mod.shutil, "which", lambda _name: "powershell")
    monkeypatch.setattr(shortcuts_mod.subprocess, "run", lambda *a, **k: _proc(2, ""))

    ok, message = shortcuts_mod._run_powershell("echo 1")

    assert ok is False
    assert "退出码 2" in message


def test_create_windows_propagates_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        shortcuts_mod, "_run_powershell", lambda script: (False, "需要管理员")
    )

    ok, message = shortcuts_mod.create(tmp_path, platform="windows")

    assert ok is False
    assert message == "需要管理员"


def test_create_windows_elevated_uses_runas(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scripts: list[str] = []
    monkeypatch.setattr(
        shortcuts_mod,
        "_run_powershell",
        lambda script: (scripts.append(script) or (True, "")),
    )

    ok, message = shortcuts_mod.create_windows_elevated(
        shortcuts_mod.DEFAULT_SHORTCUTS, tmp_path
    )

    assert ok is True
    assert "管理员" in message
    assert "RunAs" in scripts[0]


def test_create_windows_elevated_propagates_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shortcuts_mod, "_run_powershell", lambda script: (False, "被拒绝"))

    ok, message = shortcuts_mod.create_windows_elevated(
        shortcuts_mod.DEFAULT_SHORTCUTS, tmp_path
    )

    assert ok is False
    assert message == "被拒绝"


def test_create_posix_cleans_legacy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """macOS/Linux 分支也要报告清理结果，不能只有 Windows 报。"""
    (tmp_path / "打开控制台.command").write_text("old", encoding="utf-8")

    ok, message = shortcuts_mod.create(tmp_path, platform="darwin")

    assert ok is True
    assert "清理了 1 个旧快捷方式" in message


def _proc(returncode: int, text: str):  # noqa: ANN202
    from types import SimpleNamespace

    return SimpleNamespace(
        returncode=returncode, stdout=text.encode(), stderr=b""
    )


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


def test_tailscale_status_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access_mod.shutil, "which", lambda _name: None)

    assert access_mod.tailscale_status()["installed"] is False


def test_tailscale_status_parses_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        returncode = 0
        stdout = json.dumps(
            {
                "Self": {
                    "HostName": "dorm-pc",
                    "DNSName": "dorm-pc.tail12345.ts.net",
                    "TailscaleIPs": ["100.64.0.9", "fd7a::1"],
                },
                "CurrentTailnet": {"Name": "tail12345.ts.net"},
                "Peer": {"node1": {}, "node2": {}},
            }
        ).encode()

    monkeypatch.setattr(access_mod.shutil, "which", lambda _name: "tailscale")
    monkeypatch.setattr(access_mod.subprocess, "run", lambda *a, **k: Result())

    status = access_mod.tailscale_status()

    assert status["installed"] is True
    assert status["logged_in"] is True
    assert status["tailnet"] == "tail12345"
    assert status["ip"] == "100.64.0.9"
    assert status["peers"] == 2


def _fake_status(**overrides):  # noqa: ANN003
    base = {
        "installed": False,
        "running": False,
        "logged_in": False,
        "tailnet": "",
        "hostname": "",
        "ip": "",
        "peers": 0,
    }
    base.update(overrides)
    return base


def test_guide_missing_tailscale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access_mod, "tailscale_status", lambda: _fake_status())
    monkeypatch.setattr(access_mod, "lan_ip", lambda: "")

    text = "\n".join(access_mod.guide())

    assert "缺这一步" in text
    assert "winget" in text


def test_guide_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        access_mod,
        "tailscale_status",
        lambda: _fake_status(installed=True, running=True, logged_in=True,
                             tailnet="tail12345", ip="100.64.0.9", peers=3),
    )
    monkeypatch.setattr(access_mod, "lan_ip", lambda: "192.168.1.23")

    text = "\n".join(access_mod.guide())

    assert "tailnet=tail12345" in text
    assert "对端 3 台" in text
    assert "http://100.64.0.9:2268" in text


def test_cli_access_guide(monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        access_mod, "guide", lambda host="127.0.0.1", port=2268: ["三步走"]
    )

    assert cli_mod.main(["access", "--guide"]) == 0

    assert "三步走" in capsys.readouterr().out


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
