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
