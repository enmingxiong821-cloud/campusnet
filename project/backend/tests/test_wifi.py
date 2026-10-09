"""网络身份检查（wifi.py）单元测试。

netsh 全被隔离在 _run_netsh 单点，所以这里不碰真实系统命令：
monkeypatch 掉它，就能把「有线 / 热点 / 校园网」三种现场全部构造出来。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from campusnet import cli as cli_mod
from campusnet import wifi as wifi_mod

INTERFACES_ZH = """接口名称 : WLAN
    描述 : Intel(R) Wi-Fi 6 AX201 160MHz
    GUID : 8f1c2d3e-4a5b-6c7d-8e9f-0a1b2c3d4e5f
    物理地址 : a4:b1:c2:d3:e4:f5
    状态 : 已连接
    SSID : NWAFU
    BSSID : 8c:85:90:11:22:33
    网络类型 : 基础设施
    无线电类型 : 802.11ac
    信号强度 : 92%
"""

INTERFACES_EN = """Name : Wi-Fi
    Description : Intel(R) Wi-Fi 6
    GUID : 1111-2222
    Physical address : a4:b1:c2:d3:e4:f5
    State : connected
    SSID : NWAFU
    BSSID : 8c:85:90:11:22:33
    Channel : 44
"""


@pytest.fixture()
def netsh_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """拦截所有 netsh 调用，返回调用参数列表。"""
    calls: list[list[str]] = []

    def fake_run(args: list[str], timeout: float = wifi_mod.NETSH_TIMEOUT):
        calls.append(list(args))
        return True, INTERFACES_ZH

    monkeypatch.setattr(wifi_mod, "_run_netsh", fake_run)
    return calls


# ---------- current_ssid ----------


def test_current_ssid_reads_chinese_output(netsh_calls) -> None:  # noqa: ANN001
    assert wifi_mod.current_ssid() == "NWAFU"


def test_current_ssid_reads_english_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """netsh 输出随系统语言变化，英文环境下也要认。"""
    monkeypatch.setattr(
        wifi_mod, "_run_netsh", lambda args, timeout=8.0: (True, INTERFACES_EN)
    )
    assert wifi_mod.current_ssid() == "NWAFU"


def test_current_ssid_skips_bssid_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """BSSID 行的字段名形近 SSID，误读会拿到一串 MAC 地址。"""
    raw = "SSID : \nBSSID : 8c:85:90:11:22:33\n"
    monkeypatch.setattr(wifi_mod, "_run_netsh", lambda args, timeout=8.0: (True, raw))
    # SSID 段为空时不应把 BSSID 的 MAC 当成 SSID 返回
    assert wifi_mod.current_ssid() == ""


def test_current_ssid_empty_when_netsh_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        wifi_mod, "_run_netsh", lambda args, timeout=8.0: (False, "没有找到无线接口")
    )
    assert wifi_mod.current_ssid() == ""


def test_current_ssid_empty_when_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        wifi_mod, "_run_netsh", lambda args, timeout=8.0: (True, "接口名称 : WLAN\n状态 : 已连接\n")
    )
    assert wifi_mod.current_ssid() == ""


# ---------- ensure_campus_wifi ----------


def test_empty_patterns_passes_without_running_netsh(netsh_calls) -> None:  # noqa: ANN001
    """留空即关闭：一个 netsh 都不该发，否则有线用户也要被拖慢。"""
    check = wifi_mod.ensure_campus_wifi([])
    assert check.ok is True
    assert netsh_calls == []


def test_empty_patterns_passes_with_blank_strings(netsh_calls) -> None:  # noqa: ANN001
    check = wifi_mod.ensure_campus_wifi(["", ""])
    assert check.ok is True
    assert netsh_calls == []


def test_wired_connection_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """有线读不到 SSID，不能判定「不在校园网」。"""
    monkeypatch.setattr(wifi_mod, "_run_netsh", lambda args, timeout=8.0: (False, ""))
    check = wifi_mod.ensure_campus_wifi(["NWAFU"])
    assert check.ok is True
    assert check.ssid == ""


def test_matching_ssid_passes(netsh_calls) -> None:  # noqa: ANN001
    check = wifi_mod.ensure_campus_wifi(["NWAFU"])
    assert check.ok is True
    assert check.ssid == "NWAFU"


def test_ssid_match_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """校园网 SSID 常被写成 NWAFU / nwafu 两种。"""
    monkeypatch.setattr(
        wifi_mod,
        "_run_netsh",
        lambda args, timeout=8.0: (True, INTERFACES_ZH.replace("NWAFU", "nwafu")),
    )
    assert wifi_mod.ensure_campus_wifi(["NWAFU"]).ok is True
    assert wifi_mod.ensure_campus_wifi(["nwafu"]).ok is True


def test_ssid_match_is_substring(netsh_calls) -> None:  # noqa: ANN001
    """SSID 常带后缀（NWAFU-5G），按包含匹配而不是全等。"""
    assert wifi_mod.ensure_campus_wifi(["NWAFU"]).ok is True


def test_any_pattern_matches_passes(netsh_calls) -> None:  # noqa: ANN001
    """pattern 之间是「或」关系，命中任意一个即可。"""
    check = wifi_mod.ensure_campus_wifi(["OTHER-NET", "NWAFU", "THIRD"])
    assert check.ok is True


def test_hotspot_ssid_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """核心用例：连着手机热点时必须判失败，且信息要能解释后果。"""
    monkeypatch.setattr(
        wifi_mod,
        "_run_netsh",
        lambda args, timeout=8.0: (True, INTERFACES_ZH.replace("NWAFU", "MyPhone")),
    )
    check = wifi_mod.ensure_campus_wifi(["NWAFU"])
    assert check.ok is False
    assert check.ssid == "MyPhone"
    assert "MyPhone" in check.detail
    assert "NWAFU" in check.detail


def test_rejected_check_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """只返回 ok=False，不抛异常——真因该由门户报，不该被这里盖掉。"""
    monkeypatch.setattr(
        wifi_mod,
        "_run_netsh",
        lambda args, timeout=8.0: (True, INTERFACES_ZH.replace("NWAFU", "Guest")),
    )
    result = wifi_mod.ensure_campus_wifi(["NWAFU"])
    assert isinstance(result, wifi_mod.WifiCheck)
    assert result.ok is False


def test_timeout_error_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """netsh 超时抛异常时 _run_netsh 自己兜住，读不到 SSID 即按有线放行。"""

    def boom(args: list[str], timeout: float = 8.0, **_: object):
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(wifi_mod.subprocess, "run", boom)
    assert wifi_mod.current_ssid() == ""


# ---------- connect_to ----------


def test_connect_to_invokes_netsh(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(
        wifi_mod, "_run_netsh", lambda args, timeout=8.0: (seen.append(list(args)) or (True, "ok"))
    )
    ok, out = wifi_mod.connect_to("NWAFU")
    assert (ok, out) == (True, "ok")
    assert seen == [["netsh", "wlan", "connect", "name=NWAFU"]]


# ---------- CLI 暴露层 ----------


def test_show_ssid_does_not_touch_database(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """纯诊断开关：不该为了看一眼 SSID 先在磁盘落一个库文件。

    Store() 构造会 mkdir 并建表，所以早退必须发生在它之前。
    """
    monkeypatch.setattr(
        wifi_mod, "_run_netsh", lambda args, timeout=8.0: (True, INTERFACES_ZH)
    )
    assert not tmp_db.exists(), "前置：库里还没有文件"

    rc = cli_mod.main(["add", "--show-ssid"])

    assert rc == 0
    assert "当前 Wi-Fi：NWAFU" in capsys.readouterr().out
    assert not tmp_db.exists(), "--show-ssid 不该创建数据库"


def test_show_ssid_reports_wired_as_not_connected(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(wifi_mod, "_run_netsh", lambda args, timeout=8.0: (False, ""))
    assert cli_mod.main(["add", "--show-ssid"]) == 0
    assert "(未连接无线网)" in capsys.readouterr().out
    assert not tmp_db.exists()


def test_show_ssid_tolerates_bad_extra_json(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同时给了非法 --extra 时，--show-ssid 仍应正常返回，不被参数校验牵连。"""
    monkeypatch.setattr(
        wifi_mod, "_run_netsh", lambda args, timeout=8.0: (True, INTERFACES_ZH)
    )
    assert cli_mod.main(["add", "--show-ssid", "--extra", "{not json"]) == 0
    assert not tmp_db.exists()


def test_wifi_ssid_flag_lands_in_extra_fields(tmp_db: Path) -> None:
    """--wifi-ssid 省掉手写 JSON，但结果必须和手写等价。"""
    assert (
        cli_mod.main(
            [
                "add",
                "--name",
                "宿舍",
                "--portal",
                "http://10.0.0.1/eportal/portal/login",
                "--wifi-ssid",
                "NWAFU",
                "--username",
                "20240001",
                "--password",
                "campus@2024",
            ]
        )
        == 0
    )
    from campusnet.storage import Store

    acc = Store(tmp_db).list_accounts()[0]
    assert acc.extra_fields["wifi_ssid"] == "NWAFU"


def test_explicit_extra_overrides_wifi_ssid_flag(tmp_db: Path) -> None:
    """显式 JSON 是逃生舱，优先于便捷开关，不该被 --wifi-ssid 覆盖。"""
    assert (
        cli_mod.main(
            [
                "add",
                "--name",
                "宿舍",
                "--portal",
                "http://10.0.0.1/eportal/portal/login",
                "--wifi-ssid",
                "WRONG",
                "--extra",
                '{"wifi_ssid": "NWAFU"}',
                "--username",
                "20240001",
                "--password",
                "campus@2024",
            ]
        )
        == 0
    )
    from campusnet.storage import Store

    acc = Store(tmp_db).list_accounts()[0]
    assert acc.extra_fields["wifi_ssid"] == "NWAFU"