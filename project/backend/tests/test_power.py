"""远程关机 / 重启 / 唤醒：命令构造、失败处理与 WOL 包形状。"""

from __future__ import annotations

import socket
import sys

import pytest

from campusnet import cli as cli_mod
from campusnet import power


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
