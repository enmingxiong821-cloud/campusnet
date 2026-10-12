"""doctor 自检：默认只查本地状态；探活用例单独打桩，不联网。"""

from __future__ import annotations

import json
import subprocess
import sys

from campusnet import cli as cli_mod
from campusnet import doctor as doctor_mod


def _by_name(checks: list[doctor_mod.Check]) -> dict[str, doctor_mod.Check]:
    return {item.name: item for item in checks}


def test_doctor_without_accounts_warns(store) -> None:  # noqa: ANN001
    checks = doctor_mod.run(probe=False)
    account_check = _by_name(checks)["账号"]

    assert account_check.status == doctor_mod.WARN
    assert "还没有账号" in account_check.detail
    assert doctor_mod.exit_code(checks) == 0


def test_doctor_detects_live_lock_holder(store, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        lock_path = tmp_path / "held.lock"
        lock_path.write_text(f"{helper.pid}\n", encoding="utf-8")
        monkeypatch.setenv("CAMPUSNET_INSTANCE_LOCK_FILE", str(lock_path))

        check = _by_name(doctor_mod.run(probe=False))["实例锁"]

        assert check.status == doctor_mod.WARN
        assert str(helper.pid) in check.detail
    finally:
        helper.kill()
        helper.wait(timeout=10)


def test_doctor_reports_unwritable_database_dir(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    blocker = tmp_path / "blocked"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setenv("CAMPUSNET_DB", str(blocker / "x.db"))

    checks = doctor_mod.run(probe=False)

    assert _by_name(checks)["数据库"].status == doctor_mod.FAIL
    assert doctor_mod.exit_code(checks) == 1


def test_doctor_probe_uses_account_targets(store, account, monkeypatch) -> None:  # noqa: ANN001
    """探活项要覆盖多目标配置：第一个被拦、第二个放行即算在线。"""
    account.probe_url = "http://127.0.0.1:1/blocked http://127.0.0.1:2/ok"
    store.add_account(account)
    seen: list[str] = []

    def fake_probe(url: str, timeout: float = 4.0, insecure: bool = False):
        seen.append(url)
        online = url.endswith("/ok")
        return doctor_mod.probe_mod.ProbeResult(
            online, "online" if online else "unreachable", "", url
        )

    monkeypatch.setattr(doctor_mod.probe_mod, "probe", fake_probe)
    checks = doctor_mod.run(probe=True)

    probe_checks = [item for item in checks if item.name.startswith("探活 ")]
    assert probe_checks and probe_checks[0].status == doctor_mod.OK
    assert seen == ["http://127.0.0.1:1/blocked", "http://127.0.0.1:2/ok"]


def test_doctor_json_is_machine_readable(store) -> None:  # noqa: ANN001
    payload = json.loads(doctor_mod.render(doctor_mod.run(probe=False), as_json=True))

    assert any(item["name"] == "运行环境" for item in payload)
    assert all(set(item) == {"name", "status", "detail"} for item in payload)


def test_doctor_reports_notify_configuration(store) -> None:  # noqa: ANN001
    assert _by_name(doctor_mod.run(probe=False))["失败通知"].status == doctor_mod.WARN

    store.set_setting("notify_url", "http://hook.test/x")

    assert _by_name(doctor_mod.run(probe=False))["失败通知"].status == doctor_mod.OK


def test_doctor_fails_when_bootstart_would_not_apply(store, monkeypatch) -> None:  # noqa: ANN001
    """「装了但不会生效」必须报 FAIL：这正是用户重启后才发现没网的场景。"""
    from pathlib import Path

    from campusnet import bootstart as boot_mod

    monkeypatch.setattr(
        boot_mod,
        "status",
        lambda: boot_mod.BootStatus(
            True,
            True,
            Path("/tmp/campusnet.desktop"),
            "已写入 XDG autostart，但当前环境没有桌面会话，登录时不会被执行",
            True,
            effective=False,
        ),
    )

    checks = doctor_mod.run(probe=False)

    assert _by_name(checks)["开机自启动"].status == doctor_mod.FAIL
    assert doctor_mod.exit_code(checks) == 1


def test_cli_doctor_prints_report(store, capsys) -> None:  # noqa: ANN001
    assert cli_mod.main(["doctor", "--no-probe"]) == 0

    out = capsys.readouterr().out
    assert "运行环境" in out
    assert "开机自启动" in out
