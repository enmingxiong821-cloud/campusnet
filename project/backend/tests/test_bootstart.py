"""开机自启动（bootstart.py）单元测试。

三种平台的落地载体完全不同，但同一时刻只能真实运行在一种系统上。
所以这里全部走 CAMPUSNET_FORCE_PLATFORM 注入平台名，把每个分支的
生成与读写逻辑都实际执行一遍；系统命令（systemctl）一律 monkeypatch
掉，不碰真实机器状态。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from campusnet import bootstart as boot_mod
from campusnet import cli as cli_mod


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把三平台的家目录类路径全部改到临时目录，避免触碰真实用户目录。"""
    appdata = tmp_path / "appdata"
    config = tmp_path / "config"
    home = tmp_path / "home"
    for path in (appdata, config, home):
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return tmp_path


def _force(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setenv("CAMPUSNET_FORCE_PLATFORM", platform)


# ---------- platform_name ----------


def test_unknown_platform_has_no_startup_side_effects(monkeypatch):
    monkeypatch.setattr(boot_mod, "platform_name", lambda: "freebsd")
    def unexpected():
        pytest.fail("unsupported platform must not resolve startup paths")
    monkeypatch.setattr(boot_mod, "entry_path", unexpected)
    assert not boot_mod.status().supported
    assert not boot_mod.install().success
    assert not boot_mod.uninstall().success


def test_platform_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _force(monkeypatch, "darwin")
    assert boot_mod.platform_name() == "darwin"


def test_platform_override_ignores_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """乱填的值必须被忽略，否则会走进某个平台分支却带着错误预期。"""
    monkeypatch.setenv("CAMPUSNET_FORCE_PLATFORM", "plan9")
    assert boot_mod.platform_name() in ("windows", "linux", "darwin")


# ---------- 各平台文件生成 ----------


def test_windows_launcher_uses_start_and_run_script(monkeypatch: pytest.MonkeyPatch) -> None:
    _force(monkeypatch, "windows")
    text = boot_mod.windows_launcher_text(port=2268)
    assert "start " in text
    assert str(boot_mod.run_script()) in text
    assert "serve" in text and "2268" in text
    # 必须 cd 到仓库根，否则相对路径与日志落点都会跑偏
    assert f'cd /d "{boot_mod.ROOT}"' in text


def test_systemd_unit_has_restart_and_install_section(monkeypatch: pytest.MonkeyPatch) -> None:
    _force(monkeypatch, "linux")
    text = boot_mod.systemd_unit_text(port=2268)
    assert "[Service]" in text and "[Install]" in text
    assert "Restart=on-failure" in text
    assert "WantedBy=default.target" in text
    assert f"WorkingDirectory={boot_mod.ROOT}" in text


def test_xdg_desktop_is_application_type(monkeypatch: pytest.MonkeyPatch) -> None:
    _force(monkeypatch, "linux")
    text = boot_mod.xdg_desktop_text(port=2268)
    assert "Type=Application" in text
    assert "Exec=" in text
    assert "X-GNOME-Autostart-enabled=true" in text


def test_macos_plist_has_runatload(monkeypatch: pytest.MonkeyPatch) -> None:
    _force(monkeypatch, "darwin")
    text = boot_mod.macos_plist_text(port=2268)
    assert "<key>RunAtLoad</key>" in text
    assert boot_mod.LAUNCH_AGENT_LABEL in text
    # 当前只注册登录启动，不配置进程退出后的重新拉起策略。
    assert "<key>KeepAlive</key>" not in text


def test_macos_plist_escapes_special_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    _force(monkeypatch, "darwin")
    text = boot_mod.macos_plist_text(port=2268)
    assert "&" not in text.replace("&amp;", "").replace("&lt;", "").replace("&gt;", "")


# ---------- install / status / uninstall ----------


def test_windows_install_writes_entry(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, "windows")
    result = boot_mod.install(port=2268)
    assert result.installed
    assert result.path is not None and result.path.exists()
    assert result.path.name == boot_mod.WINDOWS_LAUNCHER
    assert boot_mod.status().installed


def test_windows_uninstall_removes_entry(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, "windows")
    boot_mod.install(port=2268)
    result = boot_mod.uninstall()
    assert not result.installed
    assert not boot_mod.status().installed
    # 幂等：再卸一次不报错
    assert boot_mod.uninstall().installed is False


def test_linux_install_falls_back_to_desktop_without_systemd(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, "linux")
    monkeypatch.setattr(boot_mod.shutil, "which", lambda _name: None)
    result = boot_mod.install(port=2268)
    assert result.installed
    assert result.path is not None and result.path.name == "campusnet.desktop"


def test_linux_install_uses_systemd_when_available(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, "linux")
    calls: list[list[str]] = []

    def fake_run(args, timeout=20):  # noqa: ANN001, ANN202
        calls.append(list(args))
        return True, ""

    monkeypatch.setattr(boot_mod.shutil, "which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr(boot_mod, "_run_quiet", fake_run)
    result = boot_mod.install(port=2268)
    assert result.installed
    assert boot_mod._linux_unit_path().exists()
    assert any("enable" in call for call in calls)


def test_linux_install_reports_enable_failure(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """enable 失败必须如实报未生效，不能让用户以为装好了。"""
    _force(monkeypatch, "linux")
    monkeypatch.setattr(boot_mod.shutil, "which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr(boot_mod, "_run_quiet", lambda args, timeout=20: (False, "boom"))
    result = boot_mod.install(port=2268)
    assert result.installed is False
    assert result.success is False
    assert "boom" in result.detail


def test_linux_uninstall_removes_both_carriers(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """systemd 与 desktop 两种载体都要清，否则会留一份在暗处继续拉起服务。"""
    _force(monkeypatch, "linux")
    unit = boot_mod._linux_unit_path()
    desktop = boot_mod._linux_desktop_path()
    for path in (unit, desktop):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")

    monkeypatch.setattr(boot_mod.shutil, "which", lambda _name: None)
    boot_mod.uninstall()
    assert not unit.exists() and not desktop.exists()


def test_linux_uninstall_without_unit_does_not_reload(fake_home, monkeypatch):
    """无 systemd unit 时，不应为不存在的 unit 执行 daemon-reload。"""
    _force(monkeypatch, "linux")
    monkeypatch.setattr(boot_mod.shutil, "which", lambda _: "/usr/bin/systemctl")
    calls = []
    monkeypatch.setattr(boot_mod, "_run_quiet", lambda args: calls.append(args) or (False, "bus unavailable"))
    result = boot_mod.uninstall()
    assert result.success
    assert result.installed is False
    assert calls == []


def test_linux_uninstall_desktop_only_does_not_call_systemctl(fake_home, monkeypatch):
    _force(monkeypatch, "linux")
    desktop = boot_mod._linux_desktop_path()
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text("[Desktop Entry]\n", encoding="utf-8")
    monkeypatch.setattr(boot_mod.shutil, "which", lambda _: "/usr/bin/systemctl")
    calls = []
    monkeypatch.setattr(boot_mod, "_run_quiet", lambda args: calls.append(args) or (False, "unexpected"))
    result = boot_mod.uninstall()
    assert result.success
    assert not desktop.exists()
    assert calls == []


def test_linux_uninstall_reports_reload_failure(fake_home, monkeypatch):
    _force(monkeypatch, "linux")
    unit = boot_mod._linux_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setattr(boot_mod.shutil, "which", lambda _: "/usr/bin/systemctl")
    calls = []
    def run(args):
        calls.append(args)
        return (True, "disabled") if "disable" in args else (False, "reload failed")
    monkeypatch.setattr(boot_mod, "_run_quiet", run)
    result = boot_mod.uninstall()
    assert not result.success
    assert not result.installed
    assert "reload failed" in result.detail
    assert calls[-1][-1] == "daemon-reload"


@pytest.mark.parametrize("ok,message,expected", [
    (True, "enabled", True),
    (False, "disabled", False),
    (True, "static", False),
    (False, "Failed to connect to bus", False),
])
def test_linux_status_checks_enablement(fake_home, monkeypatch, ok, message, expected):
    _force(monkeypatch, "linux")
    unit = boot_mod._linux_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setattr(boot_mod.shutil, "which", lambda _: "/usr/bin/systemctl")
    calls = []
    def run(args):
        calls.append(args)
        return ok, message
    monkeypatch.setattr(boot_mod, "_run_quiet", run)
    assert boot_mod.status().installed is expected
    assert calls == [["/usr/bin/systemctl", "--user", "is-enabled", "campusnet.service"]]


def test_linux_reload_failure_stops_enable(fake_home, monkeypatch):
    _force(monkeypatch, "linux")
    monkeypatch.setattr(boot_mod.shutil, "which", lambda _: "/usr/bin/systemctl")
    calls = []
    def run(args):
        calls.append(args)
        return False, "reload failed"
    monkeypatch.setattr(boot_mod, "_run_quiet", run)
    # 启动自检同样走 _run_quiet，这里只关心 daemon-reload 失败时的行为
    monkeypatch.setattr(boot_mod, "verify_launch", lambda: (True, ""))
    assert not boot_mod.install().installed
    assert len(calls) == 1
    assert calls[0][-1] == "daemon-reload"


def test_systemd_exec_quotes_paths(monkeypatch):
    monkeypatch.setattr(boot_mod, "launch_command", lambda host, port: [
        "/opt/python bin/python", "/opt/campus %net/run.py", "serve",
    ])
    text = boot_mod.systemd_unit_text()
    assert 'ExecStart="/opt/python bin/python" "/opt/campus %%net/run.py" "serve"' in text


def test_desktop_exec_quotes_paths(monkeypatch):
    monkeypatch.setattr(boot_mod, "launch_command", lambda host, port: [
        "/opt/python bin/python", "/opt/campus %net/run.py", "serve",
    ])
    text = boot_mod.xdg_desktop_text()
    assert 'Exec="/opt/python bin/python" "/opt/campus %%net/run.py" "serve"' in text


@pytest.mark.parametrize("host,port", [
    ("127.0.0.1 & echo injected", 2268),
    ("localhost\nExecStart=bad", 2268),
    ("127.0.0.1", 0),
    ("127.0.0.1", 65536),
])
def test_invalid_launch_arguments_do_not_write(fake_home, monkeypatch, host, port):
    _force(monkeypatch, "windows")
    result = boot_mod.install(host, port)
    assert not result.installed
    assert not result.success
    assert not boot_mod.entry_path().exists()


def test_macos_install_writes_plist(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, "darwin")
    result = boot_mod.install(port=2268)
    assert result.installed
    assert result.path is not None and result.path.suffix == ".plist"
    assert "RunAtLoad" in result.path.read_text(encoding="utf-8")


def test_status_reports_not_installed_on_clean_machine(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, "windows")
    result = boot_mod.status()
    assert result.supported and not result.installed


def test_install_overwrites_existing_entry(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重复安装要覆盖而不是报错，用户改端口后重装是常见路径。"""
    _force(monkeypatch, "windows")
    boot_mod.install(port=2268)
    second = boot_mod.install(port=2270)
    assert second.installed
    assert second.path is not None
    assert "2270" in second.path.read_text(encoding="utf-8")


# ---------- activate 文案 ----------


def test_activate_distinguishes_supported_states() -> None:
    unsupported = boot_mod.BootStatus(False, False, None, "没有可用载体")
    assert "不支持" in boot_mod.activate(unsupported)
    installed = boot_mod.BootStatus(True, True, Path("x"), "ok")
    assert "已开启" in boot_mod.activate(installed)
    missing = boot_mod.BootStatus(True, False, Path("x"), "未安装")
    assert "未开启" in boot_mod.activate(missing)


def test_as_dict_serializes_path() -> None:
    payload = boot_mod.BootStatus(True, True, Path("/tmp/x"), "ok").as_dict()
    assert payload["installed"] is True
    # 只断言落点被字符串化带出；Path 在不同系统上的分隔符规范化不是被测逻辑
    assert "tmp" in str(payload["path"])
    assert isinstance(payload["platform"], str)


# ---------- CLI ----------


def test_cli_bootstart_status_exit_zero(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _force(monkeypatch, "windows")
    assert cli_mod.main(["bootstart", "status"]) == 0
    assert "开机自启动" in capsys.readouterr().out


def test_cli_bootstart_install_then_uninstall(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _force(monkeypatch, "windows")
    assert cli_mod.main(["bootstart", "install", "--port", "2268"]) == 0
    assert "已开启" in capsys.readouterr().out
    assert cli_mod.main(["bootstart", "uninstall"]) == 0
    assert "未开启" in capsys.readouterr().out


def test_cli_bootstart_install_returns_one_on_failure(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """装不上必须给非零退出码，脚本里的 set -e 才能拦住。"""
    _force(monkeypatch, "linux")
    monkeypatch.setattr(boot_mod.shutil, "which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr(boot_mod, "_run_quiet", lambda args, timeout=20: (False, "no"))
    assert cli_mod.main(["bootstart", "install"]) == 1


# ---------- 载体生效判定与 WSL ----------


def _clear_desktop_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)


def test_is_wsl_detects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert boot_mod.is_wsl() is True

    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    if sys.platform != "linux":
        # 非 Linux 平台没有 /proc/version，不可能是 WSL
        assert boot_mod.is_wsl() is False


def test_install_refuses_on_wsl_without_systemd(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WSL 没有 systemd 也没有桌面会话，写 XDG autostart 等于没写。"""
    _force(monkeypatch, "linux")
    monkeypatch.setattr(boot_mod.shutil, "which", lambda _name: None)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setattr(boot_mod, "verify_launch", lambda: (True, ""))

    result = boot_mod.install(port=2268)

    assert result.installed is False
    assert result.success is False
    assert "WSL" in result.detail
    assert not boot_mod._linux_desktop_path().exists()


def test_install_fails_when_launch_verify_fails(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """启动自检失败就别写启动项：否则用户重启后才发现它起不来。"""
    _force(monkeypatch, "windows")
    monkeypatch.setattr(
        boot_mod, "verify_launch", lambda: (False, "ModuleNotFoundError: campusnet")
    )

    result = boot_mod.install(port=2268)

    assert result.installed is False
    assert "启动自检失败" in result.detail
    assert not boot_mod.entry_path().exists()


def test_verify_launch_passes_on_real_interpreter() -> None:
    """真的干跑一次导入：本仓库路径正确时必须通过。"""
    ok, message = boot_mod.verify_launch()

    assert ok is True, message


def test_status_reports_ineffective_desktop_entry(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有 XDG 文件但没有桌面会话，必须如实说「不会生效」。"""
    _force(monkeypatch, "linux")
    monkeypatch.setattr(boot_mod.shutil, "which", lambda _name: None)
    _clear_desktop_env(monkeypatch)
    desktop = boot_mod._linux_desktop_path()
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text("[Desktop Entry]\n", encoding="utf-8")

    result = boot_mod.status()

    assert result.installed is True
    assert result.effective is False
    assert "不会被执行" in result.detail
    assert "不会生效" in boot_mod.activate(result)


def test_status_marks_wsl_without_systemd_as_not_installed(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, "linux")
    monkeypatch.setattr(boot_mod.shutil, "which", lambda _name: None)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")

    result = boot_mod.status()

    assert result.installed is False
    assert result.effective is False
    assert "WSL" in result.detail


def test_as_dict_carries_effective() -> None:
    payload = boot_mod.BootStatus(True, True, Path("/tmp/x"), "ok", True, effective=False).as_dict()

    assert payload["effective"] is False
    assert payload["installed"] is True


# ---------- Windows 计划任务模式（开机触发、无需登录） ----------


def test_task_action_keeps_exe_and_args_separate(monkeypatch: pytest.MonkeyPatch) -> None:
    _force(monkeypatch, "windows")

    exe, args = boot_mod.task_action(host="127.0.0.1", port=2268)

    assert exe.endswith(".exe")
    assert "serve" in args
    assert "--port 2268" in args


def test_task_install_registers_and_reports_success(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, "windows")
    monkeypatch.setattr(boot_mod, "verify_launch", lambda: (True, ""))
    seen: list[tuple] = []
    monkeypatch.setattr(
        boot_mod,
        "register_task",
        lambda host, port, password: (seen.append((host, port, password)) or (True, "")),
    )

    result = boot_mod.install(port=2268, mode="task", password="secret")

    assert result.installed and result.success and result.effective
    assert seen == [("127.0.0.1", 2268, "secret")]
    assert "无需登录" in result.detail
    # 计划任务没有文件路径，提示里不该出现 None
    assert "None" not in boot_mod.activate(result)


def test_task_install_fails_without_password(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, "windows")
    monkeypatch.setattr(boot_mod, "verify_launch", lambda: (True, ""))

    result = boot_mod.install(port=2268, mode="task", password="")

    assert result.installed is False
    assert "密码" in result.detail


def test_task_mode_is_windows_only(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, "linux")
    monkeypatch.setattr(boot_mod, "verify_launch", lambda: (True, ""))

    result = boot_mod.install(port=2268, mode="task", password="x")

    assert result.installed is False
    assert "Windows" in result.detail


def test_status_prefers_registered_task(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, "windows")
    monkeypatch.setattr(boot_mod, "task_registered", lambda: True)

    result = boot_mod.status()

    assert result.installed is True
    assert result.effective is True
    assert "计划任务" in result.detail
    assert "None" not in boot_mod.activate(result)


def test_uninstall_removes_registered_task(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, "windows")
    deleted: list[bool] = []
    monkeypatch.setattr(boot_mod, "task_registered", lambda: True)
    monkeypatch.setattr(
        boot_mod, "delete_task", lambda: (deleted.append(True) or (True, ""))
    )

    result = boot_mod.uninstall()

    assert deleted == [True]
    assert result.success is True
    assert result.installed is False
    assert "计划任务" in result.detail


def test_cli_bootstart_task_install_asks_password(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force(monkeypatch, "windows")
    monkeypatch.setattr(boot_mod, "verify_launch", lambda: (True, ""))
    monkeypatch.setattr(
        boot_mod, "register_task", lambda host, port, password: (True, "")
    )
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "secret")

    assert cli_mod.main(["bootstart", "install", "--mode", "task", "--port", "2268"]) == 0
