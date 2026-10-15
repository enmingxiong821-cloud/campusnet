"""远程关机 / 重启 / 唤醒：命令构造、失败处理与 WOL 包形状。"""

from __future__ import annotations

import socket
import sys
from types import SimpleNamespace

import pytest

from campusnet import cli as cli_mod
from campusnet import power
from campusnet import powerprep


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
    monkeypatch.setattr(power, "directed_broadcast", lambda _host="": "")

    info = power.wake_info()

    assert info["macs"] == ["AC:45:EF:49:B7:F4"]
    assert "power wake --mac AC:45:EF:49:B7:F4" in str(info["command"])
    assert any("BIOS" in str(item) for item in info["notes"])  # type: ignore[union-attr]


def test_wake_info_without_mac_gives_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power, "local_macs", lambda: [])

    assert "<网卡MAC>" in str(power.wake_info()["command"])


def test_wake_info_falls_back_to_global_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    """判定不出子网时必须是那个「等于没给」的全局广播，APP 靠它来决定自己猜。"""
    monkeypatch.setattr(power, "local_macs", lambda: ["AC:45:EF:49:B7:F4"])
    monkeypatch.setattr(power, "directed_broadcast", lambda _host="": "")

    assert power.wake_info()["broadcast"] == "255.255.255.255"


def test_wake_info_keeps_directed_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power, "local_macs", lambda: ["AC:45:EF:49:B7:F4"])
    monkeypatch.setattr(
        power, "directed_broadcast", lambda host="": "10.110.255.255" if host else ""
    )

    assert power.wake_info(host="10.110.211.5")["broadcast"] == "10.110.255.255"


# --------------------------------------------------- 子网与定向广播（P2）


# 真机（中文 Windows 11）`route print -4` 的原样输出，只删掉了与本用例无关的
# 几条路由。留着中文表头是关键：认的必须是数字，不是「在链路上」这类标签。
_ROUTE_PRINT = """\
===========================================================================
接口列表
 49...........................Meta Tunnel
 11...8c 32 23 61 a3 ed ......Realtek Gaming 2.5GbE Family Controller
===========================================================================
IPv4 路由表
===========================================================================
活动路由:
网络目标        网络掩码          网关       接口   跃点数
          0.0.0.0          0.0.0.0       10.110.0.1     10.110.211.5     45
       10.110.0.0      255.255.0.0            在链路上      10.110.211.5    301
     10.110.211.5  255.255.255.255            在链路上      10.110.211.5    301
   10.110.255.255  255.255.255.255            在链路上      10.110.211.5    301
        127.0.0.0        255.0.0.0            在链路上         127.0.0.1    331
  127.255.255.255  255.255.255.255            在链路上         127.0.0.1    331
       198.18.0.0  255.255.255.252            在链路上        198.18.0.1    256
        224.0.0.0        240.0.0.0            在链路上         127.0.0.1    331
  255.255.255.255  255.255.255.255            在链路上      10.110.211.5    301
===========================================================================
永久路由:
  10.20.0.0        255.0.0.0     10.110.0.1       1
  无
"""


def _capture(outputs: dict[tuple[str, ...], str]):  # noqa: ANN202
    """按命令给假输出；没列到的命令当作不存在。"""

    def fake(args: list[str]) -> tuple[bool, str]:
        return (True, outputs[tuple(args)]) if tuple(args) in outputs else (False, "")

    return fake


def test_windows_route_table_yields_subnets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        power, "_run_capture", _capture({("route", "print", "-4"): _ROUTE_PRINT})
    )

    assert power.local_ipv4_networks("nt") == [("10.110.0.0", 16), ("198.18.0.0", 30)]


def test_windows_route_table_skips_non_subnets(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认路由、每块网卡自己的 /32、组播段、回环、以及四段的永久路由都不算子网。"""
    monkeypatch.setattr(
        power, "_run_capture", _capture({("route", "print", "-4"): _ROUTE_PRINT})
    )

    subnets = power.local_ipv4_networks("nt")

    assert ("0.0.0.0", 0) not in subnets
    assert ("10.110.211.5", 32) not in subnets
    assert ("10.110.255.255", 32) not in subnets
    assert ("127.0.0.0", 8) not in subnets
    assert ("10.20.0.0", 8) not in subnets
    assert all(prefix <= 30 for _, prefix in subnets)


def test_windows_route_table_absent_gives_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(power, "_run_capture", _capture({}))

    assert power.local_ipv4_networks("nt") == []


_IP_ADDR = """\
2: eth0    inet 10.110.211.5/16 brd 10.110.255.255 scope global eth0\\
    valid_lft forever preferred_lft forever
3: lo      inet 127.0.0.1/8 scope host lo
"""

_IFCONFIG = """\
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.5 netmask 0xffffff00 broadcast 192.168.1.255
\tinet6 fe80::1%en0 prefixlen 64 scopeid 0x4
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
"""


def test_linux_ip_addr_yields_subnets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        power,
        "_run_capture",
        _capture({("ip", "-o", "-4", "addr", "show"): _IP_ADDR}),
    )

    assert power.local_ipv4_networks("posix") == [("10.110.0.0", 16)]


def test_ifconfig_is_the_fallback_when_ip_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS 没有 `ip`；十六进制掩码要换算，回环要排掉，inet6 不能误认成 inet。"""
    monkeypatch.setattr(power, "_run_capture", _capture({("ifconfig", "-a"): _IFCONFIG}))

    assert power.local_ipv4_networks("posix") == [("192.168.1.0", 24)]


def test_ifconfig_net_tools_writes_mask_in_dotted_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        power,
        "_run_capture",
        _capture(
            {
                ("ifconfig", "-a"): (
                    "eth0: flags=4163<UP,BROADCAST,RUNNING,MULTICAST>  mtu 1500\n"
                    "        inet 172.31.32.9  netmask 255.255.240.0  broadcast 172.31.47.255\n"
                )
            }
        ),
    )

    assert power.local_ipv4_networks("posix") == [("172.31.32.0", 20)]


def test_directed_broadcast_uses_the_matching_subnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关键一条：/16 的校园网里按 /24 猜会得出 10.110.211.255，那是错的。"""
    monkeypatch.setattr(
        power, "local_ipv4_networks", lambda _platform=None: [("10.110.0.0", 16)]
    )

    assert power.directed_broadcast("10.110.211.5") == "10.110.255.255"


def test_directed_broadcast_ignores_other_subnets(monkeypatch: pytest.MonkeyPatch) -> None:
    """本机挂着 VPN / Hyper-V 时子网不止一个，认的必须是含目标地址那一个。"""
    monkeypatch.setattr(
        power,
        "local_ipv4_networks",
        lambda _platform=None: [("198.18.0.0", 30), ("10.110.0.0", 16)],
    )

    assert power.directed_broadcast("10.110.211.5") == "10.110.255.255"
    assert power.directed_broadcast("198.18.0.1") == "198.18.0.3"


def test_directed_broadcast_gives_nothing_when_it_cannot_tell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        power, "local_ipv4_networks", lambda _platform=None: [("10.110.0.0", 16)]
    )

    for host in ["8.8.8.8", "pc.tailnet.ts.net", "127.0.0.1", "", "  ", "10.110.211.5:2268"]:
        assert power.directed_broadcast(host) == "", host


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


def test_wake_rejects_out_of_range_port() -> None:
    """越界端口在 sendto 抛的是 OverflowError（不是 OSError），得先挡成 PowerError。"""
    with pytest.raises(power.PowerError) as excinfo:
        power.wake("AA:BB:CC:DD:EE:FF", port=70000)

    assert "65535" in str(excinfo.value)


def test_wake_rejects_non_numeric_port() -> None:
    with pytest.raises(power.PowerError):
        power.wake("AA:BB:CC:DD:EE:FF", port="abc")


# ------------------------------------------- CLI 分派与「电源体检」的确认路径（补测）


def test_cli_power_reboot_dry_run_reports_verb(capsys) -> None:  # noqa: ANN001
    """reboot 走的是 off 之外的 else 分支，之前没被任何用例走到。"""
    assert cli_mod.main(["power", "reboot", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "试运行" in out
    assert "重启" in out


def test_cli_power_wake_reports_target(
    monkeypatch: pytest.MonkeyPatch, capsys  # noqa: ANN001
) -> None:
    sent: list[tuple] = []

    class FakeSocket:
        def setsockopt(self, *args: object) -> None:
            pass

        def sendto(self, payload: bytes, target: tuple) -> None:
            sent.append(target)

        def close(self) -> None:
            pass

    monkeypatch.setattr(power.socket, "socket", lambda *a, **k: FakeSocket())

    code = cli_mod.main(
        ["power", "wake", "--mac", "00:11:22:33:44:55", "--broadcast", "192.168.1.255"]
    )

    assert code == 0
    assert sent == [("192.168.1.255", 9)]
    assert "192.168.1.255:9" in capsys.readouterr().out


def test_cli_power_off_wraps_power_error(
    monkeypatch: pytest.MonkeyPatch, capsys  # noqa: ANN001
) -> None:
    def boom(*args: object, **kwargs: object) -> str:
        raise power.PowerError("拒绝访问")

    monkeypatch.setattr(power, "shutdown", boom)
    # 万一上面的替换没生效，宁可测试报红也不能真把这台机器关掉。
    monkeypatch.setattr(power, "_run", lambda args: pytest.fail("不该真的执行关机命令"))

    assert cli_mod.main(["power", "off"]) == 1
    assert "操作失败" in capsys.readouterr().err


def _pending_item() -> powerprep.PowerItem:
    return powerprep.PowerItem(
        "快速启动", powerprep.WARN, "已开启，用 --fix 可关闭", fixable=True
    )


def _fake_prep_run(calls: list[bool], pending: powerprep.PowerItem):
    """记录每次 run 拿到的 fix 值；fix=True 时返回「已修复」。"""

    def fake_run(fix: bool = False, yes: bool = False, plat=None):
        calls.append(fix)
        if fix:
            return [powerprep.PowerItem("快速启动", powerprep.FIXED, "已关闭")]
        return [pending]

    return fake_run


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES", " y "])
def test_cli_power_prep_fix_asks_first_then_fixes(
    monkeypatch: pytest.MonkeyPatch, capsys, answer: str  # noqa: ANN001
) -> None:
    """--fix 不带 --yes：先只读预览，确认之后才真改。"""
    calls: list[bool] = []
    monkeypatch.setattr(powerprep, "run", _fake_prep_run(calls, _pending_item()))
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)

    assert cli_mod.main(["power", "prep", "--fix"]) == 0

    out = capsys.readouterr().out
    assert calls == [False, True], "必须先只读预览再执行修复"
    assert "将尝试自动修复" in out
    assert "已关闭" in out  # 修复后的报告


@pytest.mark.parametrize("answer", ["", "n", "no", "Y 后面还有别的内容", "yes!"])
def test_cli_power_prep_declining_never_fixes(
    monkeypatch: pytest.MonkeyPatch, capsys, answer: str  # noqa: ANN001
) -> None:
    """回答不是 y/yes 时绝不能执行修复——那条路径会改注册表与 powercfg。"""
    calls: list[bool] = []
    monkeypatch.setattr(powerprep, "run", _fake_prep_run(calls, _pending_item()))
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)

    assert cli_mod.main(["power", "prep", "--fix"]) == 0

    assert calls == [False], "未确认时不能调用 run(fix=True)"
    assert "已取消" in capsys.readouterr().out


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_cli_power_prep_without_a_tty_cancels_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, capsys, error: type[BaseException]  # noqa: ANN001
) -> None:
    """双击快捷方式 / 计划任务 / stdin 被重定向时，input 直接抛异常而不是返回空串。

    以前这里没有兜底，EOFError 会带着整条栈冒到用户面前，Ctrl-C 同理。读不到确认
    就等于没确认，必须当成取消，绝不能顺着往下跑 run(fix=True)。
    """

    def no_input(_prompt: str) -> str:
        raise error()

    calls: list[bool] = []
    monkeypatch.setattr(powerprep, "run", _fake_prep_run(calls, _pending_item()))
    monkeypatch.setattr("builtins.input", no_input)

    assert cli_mod.main(["power", "prep", "--fix"]) == 0

    assert calls == [False], "读不到确认时不能调用 run(fix=True)"
    assert "已取消" in capsys.readouterr().out


def test_cli_power_prep_yes_skips_confirmation(
    monkeypatch: pytest.MonkeyPatch, capsys  # noqa: ANN001
) -> None:
    """--yes 是给无人值守用的：不该提问（那种场合也没有终端可问）。"""
    calls: list[bool] = []
    monkeypatch.setattr(powerprep, "run", _fake_prep_run(calls, _pending_item()))
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("--yes 不该提问"))

    assert cli_mod.main(["power", "prep", "--fix", "--yes"]) == 0

    assert calls == [True]
    assert "将尝试自动修复" not in capsys.readouterr().out


def test_cli_power_prep_without_fixable_item_does_not_ask(
    monkeypatch: pytest.MonkeyPatch, capsys  # noqa: ANN001
) -> None:
    """全是 OK 项时连问都不问，直接出报告（改动前的 27-29 行分支）。"""
    items = [powerprep.PowerItem("快速启动", powerprep.OK, "已关闭，关机就是真关机")]

    def fake_run(fix: bool = False, yes: bool = False, plat=None):
        return items

    monkeypatch.setattr(powerprep, "run", fake_run)
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("无可修项不该提问"))

    assert cli_mod.main(["power", "prep", "--fix"]) == 0
    assert "已关闭，关机就是真关机" in capsys.readouterr().out


def test_wake_on_lan_unreadable_is_not_marked_fixable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PowerShell / NetAdapter 模块不可用时 --fix 也修不了，不能标成「可自动修复」。

    fixable=True 会让 CLI 的「将尝试自动修复」和控制台的确认框都把它算进去，用户
    确认一圈却什么都没发生；这条 detail 自己也写着「请手动…」，前后矛盾。
    """
    monkeypatch.setattr(powerprep, "_wake_magic_adapters", lambda: ([], []))

    item = powerprep._check_wake_on_lan(fix=False)

    assert item.status == powerprep.MANUAL
    assert item.fixable is False
    # 这就是「修不了」的证据：fix=True 走回同一条分支，状态毫无变化。
    assert powerprep._check_wake_on_lan(fix=True).status == powerprep.MANUAL
