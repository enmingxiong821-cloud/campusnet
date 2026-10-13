"""电源体检（power prep）：Windows/Linux/macOS 检查项、修复路径与 CLI 接线。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from campusnet import cli as cli_mod
from campusnet import powerprep


def _patch_run(monkeypatch: pytest.MonkeyPatch, output: str = "") -> None:
    """让所有系统命令都失败（或返回给定文本），避免测试真的执行 reg/powercfg。"""
    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (False, output))


def test_run_windows_returns_all_items(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch)
    monkeypatch.setattr(
        powerprep,
        "_check_time_skew",
        lambda: powerprep.PowerItem("时间偏差", powerprep.OK, "跳过"),
    )

    items = powerprep.run(fix=False, plat="win32")

    assert [i.name for i in items] == [
        "管理员权限",
        "快速启动",
        "网卡魔术包唤醒",
        "可唤醒设备",
        "睡眠超时",
        "远程桌面",
        "自动登录",
        "时间偏差",
        "BIOS 来电自启",
    ]
    assert all(i.status in (powerprep.OK, powerprep.WARN, powerprep.MANUAL) for i in items)


def test_run_linux_returns_items(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch)
    monkeypatch.setattr(powerprep.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        powerprep,
        "_check_time_skew",
        lambda: powerprep.PowerItem("时间偏差", powerprep.OK, "跳过"),
    )

    items = powerprep.run(fix=False, plat="linux")

    names = [i.name for i in items]
    assert "网卡 WOL" in names
    assert "定时唤醒" in names
    assert "时间偏差" in names


def test_fast_startup_ok_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powerprep, "_reg_query_value", lambda key, value: "0x0")

    assert powerprep._check_fast_startup(fix=False).status == powerprep.OK


def test_fast_startup_warns_and_fixes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powerprep, "_reg_query_value", lambda key, value: "0x1")
    monkeypatch.setattr(powerprep, "_is_admin", lambda: True)
    monkeypatch.setattr(powerprep, "_set_reg_dword", lambda key, value, data: True)

    assert powerprep._check_fast_startup(fix=False).status == powerprep.WARN
    fixed = powerprep._check_fast_startup(fix=True)
    assert fixed.status == powerprep.FIXED
    assert fixed.undo


def test_fast_startup_fix_needs_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powerprep, "_reg_query_value", lambda key, value: "0x1")
    monkeypatch.setattr(powerprep, "_is_admin", lambda: False)

    assert powerprep._check_fast_startup(fix=True).status == powerprep.MANUAL


def test_wake_on_lan_parses_powershell(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        [
            {"Name": "以太网", "WakeOnMagicPacket": "Enabled", "WakeOnPattern": "Disabled"},
            {"Name": "Wi-Fi", "WakeOnMagicPacket": "Disabled", "WakeOnPattern": "Disabled"},
        ]
    )
    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (True, payload))

    item = powerprep._check_wake_on_lan(fix=False)

    assert item.status == powerprep.WARN
    assert "Wi-Fi" in item.detail


def test_time_skew_skips_when_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("offline")

    monkeypatch.setattr(powerprep.urllib.request, "urlopen", _raise)

    item = powerprep._check_time_skew()

    assert item.status == powerprep.OK
    assert "跳过" in item.detail


def test_time_skew_warns_when_skewed(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        headers = {"Date": "Mon, 13 Oct 2020 00:00:00 GMT"}

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args) -> None:  # noqa: ANN002
            return None

    monkeypatch.setattr(powerprep.urllib.request, "urlopen", lambda *a, **k: Response())

    item = powerprep._check_time_skew()

    assert item.status == powerprep.WARN
    assert "超过 5 分钟" in item.detail


def test_render_and_exit_code() -> None:
    items = [
        powerprep.PowerItem("A", powerprep.OK, "好"),
        powerprep.PowerItem("B", powerprep.MANUAL, "要人工"),
    ]

    assert powerprep.exit_code(items) == 1
    text = powerprep.render(items)
    assert "[OK  ] A" in text
    assert "[人工] B" in text


def test_render_json() -> None:
    items = [powerprep.PowerItem("A", powerprep.OK, "好")]

    data = json.loads(powerprep.render(items, as_json=True))

    assert data[0]["name"] == "A"
    assert data[0]["status"] == "ok"


def test_cli_power_prep(monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        powerprep,
        "run",
        lambda fix=False, yes=False, plat=None: [
            powerprep.PowerItem("测试", powerprep.OK, "通过")
        ],
    )

    assert cli_mod.main(["power", "prep"]) == 0
    assert "测试" in capsys.readouterr().out


def test_cli_power_prep_json(monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        powerprep,
        "run",
        lambda fix=False, yes=False, plat=None: [
            powerprep.PowerItem("测试", powerprep.OK, "通过")
        ],
    )

    assert cli_mod.main(["power", "prep", "--json"]) == 0
    assert '"status"' in capsys.readouterr().out


def test_cli_power_prep_fix_yes(monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # noqa: ANN001
    calls: list[tuple] = []

    def fake_run(fix: bool = False, yes: bool = False, plat=None):  # noqa: ANN001, ANN002
        calls.append((fix, yes))
        return [powerprep.PowerItem("测试", powerprep.FIXED, "已修")]

    monkeypatch.setattr(powerprep, "run", fake_run)

    assert cli_mod.main(["power", "prep", "--fix", "--yes"]) == 0
    assert calls == [(True, True)]


def test_cli_doctor_power(monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        powerprep,
        "run",
        lambda fix=False, yes=False, plat=None: [
            powerprep.PowerItem("测试", powerprep.OK, "通过")
        ],
    )

    assert cli_mod.main(["doctor", "--power"]) == 0
    assert "测试" in capsys.readouterr().out


# ------------------------------------------------------- 平台分支补测（F2）
# 下面按「不修 / 修成功 / 修失败 / 无权限」四态覆盖各项，让平台相关路径
# 都有可判定的结论；全部用 stub，不真的改注册表或跑 powercfg。


def test_reg_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        powerprep, "_run", lambda args, timeout=20: (True, "  HiberbootEnabled  REG_DWORD  0x0")
    )
    assert powerprep._reg_query_value("k", "HiberbootEnabled") == "0x0"

    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (False, ""))
    assert powerprep._reg_query_value("k", "HiberbootEnabled") is None
    assert powerprep._set_reg_dword("k", "v", 0) is False

    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (True, "ok"))
    assert powerprep._set_reg_dword("k", "v", 0) is True


def test_run_helper_swallows_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise OSError("missing binary")

    monkeypatch.setattr(powerprep.subprocess, "run", boom)
    assert powerprep._run(["nope"]) == (False, "")


def test_is_admin_and_check_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (True, ""))
    assert powerprep._is_admin() is True
    assert powerprep._check_admin().status == powerprep.OK

    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (False, ""))
    assert powerprep._is_admin() is False
    assert powerprep._check_admin().status == powerprep.MANUAL


def test_fast_startup_fix_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powerprep, "_reg_query_value", lambda key, value: "0x1")
    monkeypatch.setattr(powerprep, "_is_admin", lambda: True)
    monkeypatch.setattr(powerprep, "_set_reg_dword", lambda key, value, data: False)

    item = powerprep._check_fast_startup(fix=True)
    assert item.status == powerprep.MANUAL
    assert "修改失败" in item.detail


def test_wake_magic_adapters_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    # 命令失败 / 空输出 / 坏 JSON / 非 dict 元素
    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (False, ""))
    assert powerprep._wake_magic_adapters() == ([], [])

    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (True, "   "))
    assert powerprep._wake_magic_adapters() == ([], [])

    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (True, "not json"))
    assert powerprep._wake_magic_adapters() == ([], [])

    payload = json.dumps([{"Name": ""}, "junk", {"Name": "eth0", "WakeOnMagicPacket": "Disabled"}])
    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (True, payload))
    allowed, denied = powerprep._wake_magic_adapters()
    assert allowed == [] and denied == ["eth0"]


def test_wake_on_lan_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powerprep, "_wake_magic_adapters", lambda: ([], []))
    assert powerprep._check_wake_on_lan(fix=False).status == powerprep.MANUAL


def test_wake_on_lan_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powerprep, "_wake_magic_adapters", lambda: (["eth0"], []))
    item = powerprep._check_wake_on_lan(fix=False)
    assert item.status == powerprep.OK
    assert "eth0" in item.detail


def test_wake_on_lan_fix_needs_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powerprep, "_wake_magic_adapters", lambda: ([], ["eth0"]))
    monkeypatch.setattr(powerprep, "_is_admin", lambda: False)
    assert powerprep._check_wake_on_lan(fix=True).status == powerprep.MANUAL


def test_wake_on_lan_fix_all_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powerprep, "_wake_magic_adapters", lambda: ([], ["eth0", "wlan0"]))
    monkeypatch.setattr(powerprep, "_is_admin", lambda: True)
    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (True, ""))
    item = powerprep._check_wake_on_lan(fix=True)
    assert item.status == powerprep.FIXED
    assert "eth0" in item.detail


def test_wake_on_lan_fix_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powerprep, "_wake_magic_adapters", lambda: ([], ["eth0"]))
    monkeypatch.setattr(powerprep, "_is_admin", lambda: True)
    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (False, ""))
    item = powerprep._check_wake_on_lan(fix=True)
    assert item.status == powerprep.MANUAL
    assert "eth0" in item.detail


def test_wake_armed_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (False, ""))
    assert powerprep._check_wake_armed().status == powerprep.MANUAL

    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (True, "  \n "))
    assert powerprep._check_wake_armed().status == powerprep.WARN

    text = "\n".join(f"设备 {i}" for i in range(1, 6))
    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (True, text))
    item = powerprep._check_wake_armed()
    assert item.status == powerprep.OK
    assert "…" in item.detail  # 超过 3 个时截断


def test_sleep_timeout_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (False, ""))
    assert powerprep._check_sleep_timeout(fix=False).status == powerprep.MANUAL

    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (True, "无索引"))
    assert "无法解析" in powerprep._check_sleep_timeout(fix=False).detail

    # 已是 0：OK
    monkeypatch.setattr(
        powerprep, "_run",
        lambda args, timeout=20: (True, "当前交流电源设置索引: 0x00000000"),
    )
    assert powerprep._check_sleep_timeout(fix=False).status == powerprep.OK

    # 非 0 且不提权修改：MANUAL
    mono = lambda args, timeout=20: (True, "当前交流电源设置索引: 0x0000001e")  # noqa: E731
    monkeypatch.setattr(powerprep, "_run", mono)
    monkeypatch.setattr(powerprep, "_is_admin", lambda: False)
    assert powerprep._check_sleep_timeout(fix=True).status == powerprep.MANUAL

    # 非 0 且警告（不修）
    assert powerprep._check_sleep_timeout(fix=False).status == powerprep.WARN


def test_sleep_timeout_fix_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powerprep, "_is_admin", lambda: True)
    text = "当前交流电源设置索引: 0x0000001e"
    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (True, text))
    assert powerprep._check_sleep_timeout(fix=True).status == powerprep.FIXED

    # /change 失败：MANUAL
    def selective(args, timeout=20):
        if args[:1] == ["powercfg"] and "/change" in args:
            return False, ""
        return True, text

    monkeypatch.setattr(powerprep, "_run", selective)
    assert powerprep._check_sleep_timeout(fix=True).status == powerprep.MANUAL


def test_sleep_timeout_plain_decimal(monkeypatch: pytest.MonkeyPatch) -> None:
    """不带 0x 前缀的十进制也要能解析。"""
    monkeypatch.setattr(
        powerprep, "_run", lambda args, timeout=20: (True, "当前交流电源设置索引: 30")
    )
    item = powerprep._check_sleep_timeout(fix=False)
    assert item.status == powerprep.WARN
    assert "30 分钟" in item.detail


def test_remote_desktop_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powerprep, "_reg_query_value", lambda key, value: "0x0")
    assert powerprep._check_remote_desktop(fix=False).status == powerprep.OK

    monkeypatch.setattr(powerprep, "_reg_query_value", lambda key, value: None)
    monkeypatch.setattr(powerprep, "_is_admin", lambda: False)
    assert powerprep._check_remote_desktop(fix=True).status == powerprep.MANUAL

    monkeypatch.setattr(powerprep, "_reg_query_value", lambda key, value: None)
    assert powerprep._check_remote_desktop(fix=False).status == powerprep.WARN

    monkeypatch.setattr(powerprep, "_is_admin", lambda: True)
    monkeypatch.setattr(powerprep, "_set_reg_dword", lambda key, value, data: True)
    assert powerprep._check_remote_desktop(fix=True).status == powerprep.FIXED

    monkeypatch.setattr(powerprep, "_set_reg_dword", lambda key, value, data: False)
    assert powerprep._check_remote_desktop(fix=True).status == powerprep.MANUAL


def test_auto_logon_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powerprep, "_reg_query_value", lambda key, value: "1")
    assert powerprep._check_auto_logon().status == powerprep.OK

    monkeypatch.setattr(powerprep, "_reg_query_value", lambda key, value: None)
    assert powerprep._check_auto_logon().status == powerprep.MANUAL


def test_bios_ac_recovery_reads_board(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        powerprep, "_run", lambda args, timeout=20: (True, "Manufacturer  Product\nASUS  PRIME B450M")
    )
    assert "ASUS" in powerprep._check_bios_ac_recovery().detail

    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (False, ""))
    assert "未知主板" in powerprep._check_bios_ac_recovery().detail


def test_linux_wol_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    # 没装 ethtool
    monkeypatch.setattr(powerprep.shutil, "which", lambda _name: None)
    assert powerprep._check_linux_wol(fix=False).status == powerprep.MANUAL

    monkeypatch.setattr(powerprep.shutil, "which", lambda _name: "/sbin/ethtool")

    # 列不出网卡
    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (False, ""))
    assert "无法列出网卡" in powerprep._check_linux_wol(fix=False).detail

    # 有网卡且支持 g
    def all_ok(args, timeout=20):
        if args[:3] == ["ip", "-o", "link"]:
            return True, "1: lo: <LOOPBACK> \n2: eth0: <BROADCAST>"
        return True, "Wake-on: g"

    monkeypatch.setattr(powerprep, "_run", all_ok)
    item = powerprep._check_linux_wol(fix=False)
    assert item.status == powerprep.OK and "eth0" in item.detail

    # 有网卡但不支持 g：警告
    def no_g(args, timeout=20):
        if args[:3] == ["ip", "-o", "link"]:
            return True, "2: eth0: <BROADCAST>"
        return True, "Wake-on: d"

    monkeypatch.setattr(powerprep, "_run", no_g)
    assert powerprep._check_linux_wol(fix=False).status == powerprep.WARN

    # 修成功
    def fix_ok(args, timeout=20):
        if args[:3] == ["ip", "-o", "link"]:
            return True, "2: eth0: <BROADCAST>"
        if args[:2] == ["sudo", "-n"]:
            return True, ""
        return True, "Wake-on: d"

    monkeypatch.setattr(powerprep, "_run", fix_ok)
    assert powerprep._check_linux_wol(fix=True).status == powerprep.FIXED

    # 修失败
    def fix_fail(args, timeout=20):
        if args[:3] == ["ip", "-o", "link"]:
            return True, "2: eth0: <BROADCAST>"
        if args[:2] == ["sudo", "-n"]:
            return False, ""
        return True, "Wake-on: d"

    monkeypatch.setattr(powerprep, "_run", fix_fail)
    assert powerprep._check_linux_wol(fix=True).status == powerprep.MANUAL


def test_linux_wol_no_physical_iface(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powerprep.shutil, "which", lambda _name: "/sbin/ethtool")
    monkeypatch.setattr(
        powerprep, "_run", lambda args, timeout=20: (True, "1: lo: <LOOPBACK>")
    )
    assert powerprep._check_linux_wol(fix=False).status == powerprep.MANUAL


def test_linux_rtcwake_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powerprep.shutil, "which", lambda _name: "/usr/bin/rtcwake")
    assert powerprep._check_linux_rtcwake().status == powerprep.OK

    monkeypatch.setattr(powerprep.shutil, "which", lambda _name: None)
    assert powerprep._check_linux_rtcwake().status == powerprep.MANUAL


def test_macos_womp_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (False, ""))
    assert powerprep._check_macos_womp(fix=False).status == powerprep.MANUAL

    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (True, " womp 1\n"))
    assert powerprep._check_macos_womp(fix=False).status == powerprep.OK

    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (True, " womp 0\n"))
    assert powerprep._check_macos_womp(fix=False).status == powerprep.WARN

    def fix_ok(args, timeout=20):
        if args[:2] == ["sudo", "-n"]:
            return True, ""
        return True, " womp 0\n"

    monkeypatch.setattr(powerprep, "_run", fix_ok)
    assert powerprep._check_macos_womp(fix=True).status == powerprep.FIXED

    def fix_fail(args, timeout=20):
        if args[:2] == ["sudo", "-n"]:
            return False, ""
        return True, " womp 0\n"

    monkeypatch.setattr(powerprep, "_run", fix_fail)
    assert powerprep._check_macos_womp(fix=True).status == powerprep.MANUAL


def test_time_skew_without_date_header(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(powerprep.urllib.request, "urlopen", lambda *a, **k: Response())
    assert "未返回 Date 头" in powerprep._check_time_skew().detail


def test_time_skew_unparsable_date(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        headers = {"Date": "不是一个时间"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(powerprep.urllib.request, "urlopen", lambda *a, **k: Response())
    assert "无法解析" in powerprep._check_time_skew().detail


def test_time_skew_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(timezone.utc)

    class Response:
        headers = {"Date": now.strftime("%a, %d %b %Y %H:%M:%S GMT")}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(powerprep.urllib.request, "urlopen", lambda *a, **k: Response())
    assert powerprep._check_time_skew().status == powerprep.OK


def test_run_darwin_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        powerprep,
        "_check_time_skew",
        lambda: powerprep.PowerItem("时间偏差", powerprep.OK, "跳过"),
    )
    monkeypatch.setattr(powerprep, "_run", lambda args, timeout=20: (False, ""))
    names = [i.name for i in powerprep.run(fix=False, plat="darwin")]
    assert names == ["网卡唤醒", "时间偏差"]


def test_exit_code_fixed_is_zero() -> None:
    assert powerprep.exit_code([powerprep.PowerItem("a", powerprep.FIXED, "")]) == 0
    assert powerprep.exit_code([powerprep.PowerItem("a", powerprep.WARN, "")]) == 0
    assert powerprep.exit_code([powerprep.PowerItem("a", powerprep.MANUAL, "")]) == 1
