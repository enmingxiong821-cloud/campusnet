"""远程关机 / 重启 / 唤醒：命令构造、失败处理与 WOL 包形状。"""

from __future__ import annotations

import socket
import sys
from types import SimpleNamespace

import pytest

from campusnet import cli as cli_mod
from campusnet import power


def _proc(returncode: int = 0, stdout: str = "") -> SimpleNamespace:
    """假的 subprocess 结果对象；_run / _run_capture 只读这三项。"""
    return SimpleNamespace(
        returncode=returncode, stdout=stdout.encode(), stderr=b""
    )


def test_shutdown_command_matches_platform() -> None:
    command = power.shutdown_command(30)

    if sys.platform == "win32":
        assert command[:2] == ["shutdown", "/s"]
        assert "30" in command
    else:
        assert command[0] in ("systemctl", "shutdown", "osascript")


def test_reboot_command_matches_platform() -> None:
    command = power.reboot_command()

    if sys.platform == "win32":
        assert command[:2] == ["shutdown", "/r"]
    else:
        assert command[0] in ("systemctl", "shutdown", "osascript")


def test_dry_run_does_not_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[list[str]] = []
    monkeypatch.setattr(power, "_run", lambda args: called.append(list(args)))

    text = power.shutdown(0, dry_run=True)

    assert called == []
    assert text


def test_shutdown_executes_and_reports_command(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[list[str]] = []
    monkeypatch.setattr(power, "_run", lambda args: called.append(list(args)))

    text = power.shutdown(5)

    assert called and called[0] == power.shutdown_command(5)
    assert text == " ".join(called[0])


def test_run_wraps_command_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        returncode = 1
        stdout = b""
        stderr = "拒绝访问".encode("utf-8")

    monkeypatch.setattr(power.subprocess, "run", lambda *a, **k: Result())

    with pytest.raises(power.PowerError, match="拒绝访问"):
        power._run(["shutdown", "/s"])


def test_magic_packet_shape() -> None:
    packet = power.magic_packet("AA:BB:CC:DD:EE:FF")

    assert len(packet) == 6 + 16 * 6
    assert packet[:6] == b"\xff" * 6
    assert packet[6:12] == bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF])


def test_magic_packet_rejects_bad_mac() -> None:
    with pytest.raises(power.PowerError, match="MAC"):
        power.magic_packet("AA:BB")


def test_wake_sends_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple] = []
    options: list[tuple] = []

    class FakeSocket:
        def setsockopt(self, level: int, name: int, value: int) -> None:
            options.append((level, name, value))

        def sendto(self, payload: bytes, target: tuple) -> None:
            sent.append((payload, target))

        def close(self) -> None:
            sent.append(("closed", None))

    monkeypatch.setattr(power.socket, "socket", lambda *a, **k: FakeSocket())

    target = power.wake("00:11:22:33:44:55", "192.168.1.255", 9)

    assert target == "192.168.1.255:9"
    assert sent[0][1] == ("192.168.1.255", 9)
    assert sent[0][0][:6] == b"\xff" * 6
    assert (socket.SOL_SOCKET, socket.SO_BROADCAST, 1) in options


def test_cli_power_off_dry_run(capsys) -> None:  # noqa: ANN001
    assert cli_mod.main(["power", "off", "--dry-run"]) == 0

    assert "试运行" in capsys.readouterr().out


def test_cli_power_wake_requires_mac(capsys) -> None:  # noqa: ANN001
    assert cli_mod.main(["power", "wake"]) == 2

    assert "需要 --mac" in capsys.readouterr().err


def test_local_macs_parses_getmac(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 上从 getmac 的 CSV 输出里取 MAC，统一成冒号大写形式。"""
    monkeypatch.setattr(power.shutil, "which", lambda _name: "getmac")
    monkeypatch.setattr(
        power,
        "_run_capture",
        lambda args: (
            True,
            '"AC-45-EF-49-B7-F4","\\Device\\Tcpip_{X}"\n"8C-32-23-61-A3-ED","Media disconnected"\n',
        ),
    )

    assert power.local_macs("nt") == ["AC:45:EF:49:B7:F4", "8C:32:23:61:A3:ED"]


def test_local_macs_returns_empty_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power.shutil, "which", lambda _name: None)

    assert power.local_macs("nt") == []


def test_wake_info_carries_mac_command_and_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(power, "local_macs", lambda: ["AC:45:EF:49:B7:F4"])

    info = power.wake_info()

    assert info["macs"] == ["AC:45:EF:49:B7:F4"]
    assert "power wake --mac AC:45:EF:49:B7:F4" in str(info["command"])
    assert any("BIOS" in str(item) for item in info["notes"])  # type: ignore[union-attr]


def test_wake_info_without_mac_gives_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power, "local_macs", lambda: [])

    assert "<网卡MAC>" in str(power.wake_info()["command"])


# ------------------------------------------------------- 平台与异常分支补测（F2）


def test_run_wraps_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """命令根本跑不起来（找不到可执行文件）也要包成 PowerError。"""

    def boom(*args, **kwargs):
        raise OSError("no such binary")

    monkeypatch.setattr(power.subprocess, "run", boom)

    with pytest.raises(power.PowerError) as excinfo:
        power._run(["nope"])

    assert "OSError" in str(excinfo.value)


def test_shutdown_command_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power.sys, "platform", "darwin")
    monkeypatch.setattr(power, "_macos_gui_session", lambda: True)

    assert power.shutdown_command() == [
        "osascript",
        "-e",
        'tell application "System Events" to shut down',
    ]


def test_reboot_command_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power.sys, "platform", "darwin")
    monkeypatch.setattr(power, "_macos_gui_session", lambda: True)

    assert power.reboot_command() == [
        "osascript",
        "-e",
        'tell application "System Events" to restart',
    ]


def test_darwin_without_gui_session_uses_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LaunchDaemon / SSH 场景：没有 Aqua 会话，osascript 会静默失败。

    这正是「无需登录」的目标场景，所以必须回退到 shutdown 而不是照发 osascript。
    """
    monkeypatch.setattr(power.sys, "platform", "darwin")
    monkeypatch.setattr(power, "_macos_gui_session", lambda: False)

    assert power.shutdown_command() == ["shutdown", "-h", "now"]
    assert power.reboot_command() == ["shutdown", "-r", "now"]


def test_macos_gui_session_detects_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 50000 10.0.0.1 22")
    monkeypatch.setattr(power.shutil, "which", lambda _name: "/bin/launchctl")

    assert power._macos_gui_session() is False


def test_macos_gui_session_on_plain_ssh_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setenv("SSH_TTY", "/dev/ttys003")
    monkeypatch.setattr(power.shutil, "which", lambda _name: "/bin/launchctl")

    assert power._macos_gui_session() is False


def test_macos_gui_session_without_launchctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(power.shutil, "which", lambda _name: None)

    assert power._macos_gui_session() is False


def test_macos_gui_session_reads_launchctl_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(power.shutil, "which", lambda _name: "/bin/launchctl")

    monkeypatch.setattr(power, "_run_capture", lambda _args: (True, "Aqua\n"))
    assert power._macos_gui_session() is True

    # LaunchDaemon 是 System，SSH 是 Background，都不算图形会话。
    monkeypatch.setattr(power, "_run_capture", lambda _args: (True, "System\n"))
    assert power._macos_gui_session() is False

    monkeypatch.setattr(power, "_run_capture", lambda _args: (False, ""))
    assert power._macos_gui_session() is False


def test_linux_uses_systemctl_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power.sys, "platform", "linux")
    monkeypatch.setattr(power.shutil, "which", lambda _name: "/usr/bin/systemctl")

    assert power.shutdown_command() == ["systemctl", "poweroff"]
    assert power.reboot_command() == ["systemctl", "reboot"]


def test_linux_falls_back_to_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power.sys, "platform", "linux")
    monkeypatch.setattr(power.shutil, "which", lambda _name: None)

    assert power.shutdown_command() == ["shutdown", "-h", "now"]
    assert power.reboot_command() == ["shutdown", "-r", "now"]


def test_delay_is_clamped_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power.sys, "platform", "win32")

    assert power.shutdown_command(-5) == ["shutdown", "/s", "/t", "0"]


def test_reboot_executes_and_reports_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(power.sys, "platform", "win32")
    monkeypatch.setattr(power, "_run", lambda args: calls.append(args))

    assert power.reboot(30) == "shutdown /r /t 30"
    assert calls == [["shutdown", "/r", "/t", "30"]]


def test_reboot_dry_run_skips_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power.sys, "platform", "win32")
    monkeypatch.setattr(
        power, "_run", lambda args: pytest.fail("dry_run 不该真的执行命令")
    )

    assert power.reboot(0, dry_run=True) == "shutdown /r /t 0"


def test_run_capture_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power.subprocess, "run", lambda *a, **k: _proc(1, "出错"))

    ok, text = power._run_capture(["x"])

    assert ok is False
    assert "出错" in text


def test_run_capture_swallows_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise OSError("missing")

    monkeypatch.setattr(power.subprocess, "run", boom)

    assert power._run_capture(["x"]) == (False, "")


def test_local_macs_windows_getmac_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power.shutil, "which", lambda _name: None)

    assert power.local_macs("nt") == []


def test_local_macs_windows_command_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power.shutil, "which", lambda _name: "getmac")
    monkeypatch.setattr(power, "_run_capture", lambda args: (False, ""))

    assert power.local_macs("nt") == []


def test_local_macs_windows_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一张网卡在 getmac 里可能出现多行，去重且已连接的排前面。"""
    monkeypatch.setattr(power.shutil, "which", lambda _name: "getmac")
    monkeypatch.setattr(
        power,
        "_run_capture",
        lambda args: (
            True,
            '"AC-45-EF-49-B7-F4","Media disconnected"\n'
            '"AC-45-EF-49-B7-F4","\\Device\\Tcpip_{X}"\n'
            "这行没有 MAC\n",
        ),
    )

    assert power.local_macs("nt") == ["AC:45:EF:49:B7:F4"]


def test_local_macs_posix_parses_ip_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        power,
        "_run_capture",
        lambda args: (
            True,
            "2: eth0: <BROADCAST> link/ether ac:45:ef:49:b7:f4 brd ff:ff:ff:ff:ff:ff\n"
            "3: lo: <LOOPBACK> link/ether 00:00:00:00:00:00 brd 00:00:00:00:00:00\n",
        ),
    )

    assert power.local_macs("posix") == ["AC:45:EF:49:B7:F4"]


def test_local_macs_posix_falls_back_to_ifconfig(monkeypatch: pytest.MonkeyPatch) -> None:
    def capture(args):
        if args[0] == "ip":
            return False, ""
        return True, "ether ac:45:ef:49:b7:f4\n"

    monkeypatch.setattr(power, "_run_capture", capture)

    assert power.local_macs("posix") == ["AC:45:EF:49:B7:F4"]


def test_local_macs_posix_all_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power, "_run_capture", lambda args: (False, ""))

    assert power.local_macs("posix") == []


def test_local_macs_defaults_to_current_os(monkeypatch: pytest.MonkeyPatch) -> None:
    """不传 platform 时按 os.name 选实现，这里只断言它确实读了 os.name。"""
    monkeypatch.setattr(power.os, "name", "posix")
    monkeypatch.setattr(power, "_run_capture", lambda args: (False, ""))

    assert power.local_macs() == []


def test_wake_wraps_socket_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class BoomSock:
        def setsockopt(self, *args):
            pass

        def sendto(self, *args):
            raise OSError("network unreachable")

        def close(self):
            pass

    monkeypatch.setattr(power.socket, "socket", lambda *a, **k: BoomSock())

    with pytest.raises(power.PowerError) as excinfo:
        power.wake("AA:BB:CC:DD:EE:FF")

    assert "OSError" in str(excinfo.value)
