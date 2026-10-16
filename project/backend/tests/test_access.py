"""access.py（多端访问信息）的补测：覆盖 tailscale 检测、网卡选取与向导的降级分支。

这些函数本来只为「手机怎么连」服务，但 tailscale_status 的异常 / 非零退出 /
JSON 解码失败、以及 lan_ip 的接口枚举失败都是真实会走到的降级路径。用 stub
覆盖，不碰真网络与真 tailscale。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from campusnet import access


def _result(returncode: int = 0, stdout: bytes = b"") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=b"")


def _empty_status(**overrides):
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


# ---- lan_ip / local_ipv4_addresses ----

# 真机（中文 Windows 11 + FlClash）的输出片段：TUN 的跃点是 0，所以默认路由
# 出口是 198.18.0.1，而真正能用的局域网地址在 WLAN 那块。
_NETSH_ADDRESSES = """\

接口 "以太网" 的配置
    DHCP 已启用:                          是
    InterfaceMetric:                      5

接口 "本地连接* 10" 的配置
    DHCP 已启用:                          是
    IP 地址:                           169.254.243.104
    InterfaceMetric:                      25

接口 "WLAN" 的配置
    DHCP 已启用:                          是
    IP 地址:                           10.110.211.5
    子网前缀:                        10.110.0.0/16 (掩码 255.255.0.0)
    默认网关:                         10.110.0.1
    网关跃点数:                       0
    InterfaceMetric:                      40

接口 "Meta Tunnel" 的配置
    DHCP 已启用:                          否
    IP 地址:                           198.18.0.1
    子网前缀:                        198.18.0.0/30 (掩码 255.255.255.252)
    InterfaceMetric:                      0

接口 "vEthernet (Default Switch)" 的配置
    DHCP 已启用:                          否
    IP 地址:                           172.28.176.1
    子网前缀:                        172.28.176.0/20 (掩码 255.255.240.0)
    InterfaceMetric:                      5000
"""

_IP_ADDR_SHOW = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: wlan0    inet 10.110.211.5/16 brd 10.110.255.255 scope global dynamic wlan0\\       valid_lft 604343sec preferred_lft 604343sec
3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever preferred_lft forever
5: tun0    inet 198.18.0.1/30 scope global tun0\\       valid_lft forever preferred_lft forever
"""

_IFCONFIG = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.5 netmask 0xffffff00 broadcast 192.168.1.255
utun3: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
\tinet 198.18.0.1 --> 198.18.0.2 netmask 0xffffffff
awdl0: flags=8943<UP,BROADCAST,RUNNING,PROMISC,SIMPLEX,MULTICAST> mtu 1484
\tinet 169.254.72.13 netmask 0xffff0000 broadcast 169.254.255.255
"""


def _capture(outputs: dict[tuple[str, ...], str]):  # noqa: ANN202
    """按命令给假输出；没列到的命令当作不存在。"""

    def fake(args: list[str], timeout: float = 10) -> tuple[bool, str]:
        return (True, outputs[tuple(args)]) if tuple(args) in outputs else (False, "")

    return fake


def _addresses(*pairs: tuple[str, str]):  # noqa: ANN202
    return lambda _platform=None: list(pairs)


def test_windows_addresses_parsed_and_sorted_by_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """名字取块头引号、地址取独占一行的点分十进制，按 InterfaceMetric 升序。"""
    monkeypatch.setattr(
        access,
        "_run_text",
        _capture({("netsh", "interface", "ipv4", "show", "addresses"): _NETSH_ADDRESSES}),
    )

    assert access.local_ipv4_addresses("nt") == [
        ("Meta Tunnel", "198.18.0.1"),
        ("本地连接* 10", "169.254.243.104"),
        ("WLAN", "10.110.211.5"),
        ("vEthernet (Default Switch)", "172.28.176.1"),
    ]


def test_windows_addresses_command_missing_gives_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(access, "_run_text", _capture({}))

    assert access.local_ipv4_addresses("nt") == []


def test_posix_addresses_from_ip_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        access,
        "_run_text",
        _capture({("ip", "-o", "-4", "addr", "show"): _IP_ADDR_SHOW}),
    )

    assert access.local_ipv4_addresses("posix") == [
        ("lo", "127.0.0.1"),
        ("wlan0", "10.110.211.5"),
        ("docker0", "172.17.0.1"),
        ("tun0", "198.18.0.1"),
    ]


def test_posix_addresses_falls_back_to_ifconfig(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS 没有 `ip`，只能按 ifconfig 的块头认网卡名。"""
    monkeypatch.setattr(access, "_run_text", _capture({("ifconfig", "-a"): _IFCONFIG}))

    assert access.local_ipv4_addresses("posix") == [
        ("lo0", "127.0.0.1"),
        ("en0", "192.168.1.5"),
        ("utun3", "198.18.0.1"),
        ("awdl0", "169.254.72.13"),
    ]


def test_posix_addresses_commands_missing_gives_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(access, "_run_text", _capture({}))

    assert access.local_ipv4_addresses("posix") == []


def test_run_text_swallows_subprocess_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args, **_kwargs):
        raise OSError("command not found")

    monkeypatch.setattr(access.subprocess, "run", boom)

    assert access._run_text(["netsh"]) == (False, "")


def test_is_lan_address_rejects_non_lan() -> None:
    assert access._is_lan_address("10.110.211.5")
    assert not access._is_lan_address("127.0.0.1")
    assert not access._is_lan_address("169.254.243.104")
    assert not access._is_lan_address("198.18.0.1")
    assert not access._is_lan_address("0.0.0.0")
    assert not access._is_lan_address("239.255.255.250")
    assert not access._is_lan_address("不是地址")


def test_lan_ip_skips_virtual_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认路由出口被 TUN 抢走（198.18.0.1）时，报的必须是真网卡地址。"""
    monkeypatch.setattr(
        access,
        "local_ipv4_addresses",
        _addresses(
            ("Meta Tunnel", "198.18.0.1"),
            ("vEthernet (Default Switch)", "172.28.176.1"),
            ("WLAN", "10.110.211.5"),
        ),
    )
    monkeypatch.setattr(access, "_exit_ip", lambda: "198.18.0.1")

    assert access.lan_ip() == "10.110.211.5"


def test_lan_ip_prefers_exit_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    """有线 + 无线都在线时，取系统自己偏好（默认路由出口）的那块。"""
    monkeypatch.setattr(
        access,
        "local_ipv4_addresses",
        _addresses(("以太网", "192.168.1.5"), ("WLAN", "10.110.211.5")),
    )
    monkeypatch.setattr(access, "_exit_ip", lambda: "10.110.211.5")

    assert access.lan_ip() == "10.110.211.5"


def test_lan_ip_keeps_private_ranges_used_by_campus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """172.16/12 是 Hyper-V/WSL 的常用段，但校园网也可能真在用，只能按名字排。"""
    monkeypatch.setattr(
        access,
        "local_ipv4_addresses",
        _addresses(("以太网", "172.20.5.9"), ("vEthernet (WSL)", "172.28.176.1")),
    )
    monkeypatch.setattr(access, "_exit_ip", lambda: "")

    assert access.lan_ip() == "172.20.5.9"


def test_lan_ip_all_virtual_gives_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """只剩虚拟网卡时宁可什么都不报，也不打印一个手机连不上的地址。"""
    monkeypatch.setattr(
        access,
        "local_ipv4_addresses",
        _addresses(("Meta Tunnel", "198.18.0.1"), ("本地连接* 9", "169.254.68.126")),
    )
    monkeypatch.setattr(access, "_exit_ip", lambda: "198.18.0.1")

    assert access.lan_ip() == ""


def test_lan_ip_falls_back_when_enumeration_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """枚举命令跑不通（精简系统）时退回老办法，不比原来差。"""
    monkeypatch.setattr(access, "local_ipv4_addresses", lambda _platform=None: [])
    monkeypatch.setattr(access, "_exit_ip", lambda: "10.0.0.7")

    assert access.lan_ip() == "10.0.0.7"


def test_exit_ip_returns_sockname(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSock:
        def __init__(self, *args, **kwargs):
            pass

        def settimeout(self, _timeout):
            pass

        def connect(self, _addr):
            pass

        def getsockname(self):
            return ("10.0.0.7", 0)

        def close(self):
            pass

    monkeypatch.setattr(access.socket, "socket", FakeSock)

    assert access._exit_ip() == "10.0.0.7"


def test_exit_ip_swallows_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    class BoomSock:
        def __init__(self, *args, **kwargs):
            pass

        def settimeout(self, _timeout):
            pass

        def connect(self, _addr):
            raise OSError("no route")

        def close(self):
            pass

    monkeypatch.setattr(access.socket, "socket", BoomSock)

    assert access._exit_ip() == ""


def test_is_virtual_name_keeps_localized_names() -> None:
    """中文系统上真网卡叫「以太网」「WLAN」，不该被当成虚拟网卡。"""
    assert access._is_virtual_name("vEthernet (Default Switch)")
    assert access._is_virtual_name("Meta Tunnel")
    assert access._is_virtual_name("Hyper-V Virtual Ethernet Adapter")
    assert not access._is_virtual_name("以太网")
    assert not access._is_virtual_name("WLAN")
    assert not access._is_virtual_name("Wi-Fi")


# ---- tailscale_ip ----


def test_tailscale_ip_missing(monkeypatch):
    monkeypatch.setattr(access.shutil, "which", lambda _name: None)
    assert access.tailscale_ip() == ""


def test_tailscale_ip_ok(monkeypatch):
    monkeypatch.setattr(access.shutil, "which", lambda _name: "/usr/bin/tailscale")
    monkeypatch.setattr(
        access.subprocess, "run", lambda *a, **k: _result(0, b"100.64.0.5\n")
    )
    assert access.tailscale_ip() == "100.64.0.5"


def test_tailscale_ip_nonzero(monkeypatch):
    monkeypatch.setattr(access.shutil, "which", lambda _name: "/usr/bin/tailscale")
    monkeypatch.setattr(access.subprocess, "run", lambda *a, **k: _result(1, b""))
    assert access.tailscale_ip() == ""


def test_tailscale_ip_raises(monkeypatch):
    monkeypatch.setattr(access.shutil, "which", lambda _name: "/usr/bin/tailscale")

    def boom(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(access.subprocess, "run", boom)
    assert access.tailscale_ip() == ""


# ---- token_text ----


def test_token_text_returns_token(monkeypatch):
    class FakeAuth:
        token = "abc123"

    monkeypatch.setattr(access, "ApiAuth", lambda: FakeAuth())
    assert access.token_text() == "abc123"


def test_token_text_swallows_error(monkeypatch):
    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(access, "ApiAuth", boom)
    assert access.token_text() == ""


# ---- tailscale_status ----


def test_tailscale_status_not_installed(monkeypatch):
    monkeypatch.setattr(access.shutil, "which", lambda _name: None)
    assert access.tailscale_status() == _empty_status()


def test_tailscale_status_raises(monkeypatch):
    monkeypatch.setattr(access.shutil, "which", lambda _name: "/usr/bin/tailscale")

    def boom(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(access.subprocess, "run", boom)
    got = access.tailscale_status()
    assert got["installed"] is True
    assert got["running"] is False
    assert got["logged_in"] is False


def test_tailscale_status_nonzero(monkeypatch):
    monkeypatch.setattr(access.shutil, "which", lambda _name: "/usr/bin/tailscale")
    monkeypatch.setattr(access.subprocess, "run", lambda *a, **k: _result(1, b""))
    got = access.tailscale_status()
    assert got["installed"] is True
    assert got["running"] is False


def test_tailscale_status_ok(monkeypatch):
    monkeypatch.setattr(access.shutil, "which", lambda _name: "/usr/bin/tailscale")
    payload = {
        "Self": {
            "TailscaleIPs": ["100.64.0.5", "fd7a::1"],
            "HostName": "myhost",
            "DNSName": "myhost.tailnet.ts.net.",
        },
        "Peer": {"a": {}, "b": {}},
        "CurrentTailnet": {"Name": "my-tailnet.ts.net"},
    }
    monkeypatch.setattr(
        access.subprocess, "run", lambda *a, **k: _result(0, json.dumps(payload).encode())
    )
    got = access.tailscale_status()
    assert got == {
        "installed": True,
        "running": True,
        "logged_in": True,
        "tailnet": "my-tailnet",
        "hostname": "myhost",
        "ip": "100.64.0.5",
        "peers": 2,
    }


def test_tailscale_status_bad_json(monkeypatch):
    monkeypatch.setattr(access.shutil, "which", lambda _name: "/usr/bin/tailscale")
    monkeypatch.setattr(access.subprocess, "run", lambda *a, **k: _result(0, b"not json"))
    got = access.tailscale_status()
    assert got["installed"] is True
    assert got["peers"] == 0


def test_tailscale_status_non_dict(monkeypatch):
    monkeypatch.setattr(access.shutil, "which", lambda _name: "/usr/bin/tailscale")
    monkeypatch.setattr(access.subprocess, "run", lambda *a, **k: _result(0, b"[]"))
    got = access.tailscale_status()
    assert got["installed"] is True
    assert got["peers"] == 0


# ---- _tailnet_name ----


def test_tailnet_name_strips_suffix():
    assert access._tailnet_name({"CurrentTailnet": {"Name": "foo.ts.net"}}) == "foo"


def test_tailnet_name_keeps_plain_name():
    assert access._tailnet_name({"CurrentTailnet": {"Name": "foo"}}) == "foo"


def test_tailnet_name_dns_fallback():
    assert (
        access._tailnet_name({"Self": {"DNSName": "host.tailnet.ts.net."}}) == "tailnet"
    )


def test_tailnet_name_empty():
    assert access._tailnet_name({}) == ""


# ---- guide ----


def test_guide_not_installed(monkeypatch):
    monkeypatch.setattr(access, "tailscale_status", lambda: _empty_status())
    monkeypatch.setattr(access, "lan_ip", lambda: "10.0.0.7")
    lines = access.guide(host="127.0.0.1")
    assert any("缺这一步" in line for line in lines)


def test_guide_not_logged_in(monkeypatch):
    monkeypatch.setattr(
        access, "tailscale_status", lambda: _empty_status(installed=True)
    )
    lines = access.guide()
    assert any("未登录" in line for line in lines)


def test_guide_logged_in_with_ip(monkeypatch):
    monkeypatch.setattr(
        access,
        "tailscale_status",
        lambda: _empty_status(
            installed=True, logged_in=True, tailnet="foo", ip="100.64.0.5", peers=3
        ),
    )
    monkeypatch.setattr(access, "lan_ip", lambda: "")
    lines = access.guide(host="0.0.0.0")
    assert any("Tailscale 已登录" in line for line in lines)
    assert any("100.64.0.5" in line for line in lines)


# ---- report ----


def test_report_lists_addresses_and_token(monkeypatch):
    monkeypatch.setattr(access, "lan_ip", lambda: "10.0.0.7")
    monkeypatch.setattr(access, "tailscale_ip", lambda: "100.64.0.5")
    monkeypatch.setattr(access, "token_text", lambda: "abc")
    lines = access.report(host="0.0.0.0")
    assert any("10.0.0.7" in line for line in lines)
    assert any("100.64.0.5" in line for line in lines)
    assert any("abc" in line for line in lines)
