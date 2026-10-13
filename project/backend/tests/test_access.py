"""access.py（多端访问信息）的补测：覆盖 tailscale 检测与向导的各降级分支。

这些函数本来只为「手机怎么连」服务，但 tailscale_status 的异常 / 非零退出 /
JSON 解码失败都是真实会走到的降级路径。用 stub 覆盖，不碰真网络与真 tailscale。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

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


# ---- lan_ip ----


def test_lan_ip_returns_sockname(monkeypatch):
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
    assert access.lan_ip() == "10.0.0.7"


def test_lan_ip_swallows_oserror(monkeypatch):
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
    assert access.lan_ip() == ""


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
