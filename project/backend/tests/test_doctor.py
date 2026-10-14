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
    """有别的进程真的持着锁时，doctor 要报 WARN 并说出持有者 PID。

    判定以内核文件锁为准：光写一个 PID 文件不算「持有」（PID 会被系统复用，
    那种判法会把服务永久挡在门外），所以这里必须让子进程真的去 acquire。
    """
    import textwrap
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]  # project/backend，campusnet 可导入
    lock_path = tmp_path / "held.lock"
    helper_code = textwrap.dedent(
        """
        import os, sys, time
        sys.path.insert(0, sys.argv[1])
        from campusnet.lockfile import SingleInstance
        lock = SingleInstance(sys.argv[2])
        print('ACQUIRED' if lock.acquire() else 'BLOCKED', flush=True)
        print(os.getpid(), flush=True)
        time.sleep(30)
        """
    )
    helper = subprocess.Popen(
        [sys.executable, "-c", helper_code, str(backend), str(lock_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert helper.stdout.readline().strip() == "ACQUIRED"
        # 用子进程自报的 PID，不用 helper.pid：Windows 上 venv 的 python.exe 是个启动器，
        # 真正跑解释器的是它拉起的子进程，Popen.pid 与锁文件里记的那个根本不是一个数。
        # 原先断言 helper.pid 等于在赌「两者必相等」，这个前提在 venv 里不成立。
        holder_pid = helper.stdout.readline().strip()
        monkeypatch.setenv("CAMPUSNET_INSTANCE_LOCK_FILE", str(lock_path))

        check = _by_name(doctor_mod.run(probe=False))["实例锁"]

        assert check.status == doctor_mod.WARN
        assert holder_pid in check.detail
    finally:
        helper.terminate()
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


def test_render_lists_failed_checks_at_the_end() -> None:
    """满屏几十行里 FAIL 容易被划过去，末尾必须再点名一次。"""
    checks = [
        doctor_mod.Check("运行环境", doctor_mod.OK, "Python 3.12"),
        doctor_mod.Check("服务端口", doctor_mod.FAIL, "2268 被占用"),
        doctor_mod.Check("失败通知", doctor_mod.WARN, "未配置"),
        doctor_mod.Check("数据目录", doctor_mod.FAIL, "不可写"),
    ]

    text = doctor_mod.render(checks)

    assert text.rstrip().endswith("需要先处理这 2 项：服务端口、数据目录")
    # 检查顺序是刻意排的，渲染不该重排。
    assert text.index("运行环境") < text.index("服务端口") < text.index("数据目录")


def test_render_stays_quiet_when_nothing_failed() -> None:
    checks = [
        doctor_mod.Check("运行环境", doctor_mod.OK, "Python 3.12"),
        doctor_mod.Check("失败通知", doctor_mod.WARN, "未配置"),
    ]

    text = doctor_mod.render(checks)

    assert "需要先处理" not in text
    assert text.count("\n") == len(checks) - 1


def test_render_json_has_no_summary_line() -> None:
    """摘要只属于人看的文本；机器读的 JSON 保持 [{name,status,detail}]。"""
    checks = [doctor_mod.Check("服务端口", doctor_mod.FAIL, "占用")]

    payload = json.loads(doctor_mod.render(checks, as_json=True))

    assert payload == [{"name": "服务端口", "status": "fail", "detail": "占用"}]


def test_compare_versions_handles_prefixes() -> None:
    assert doctor_mod.compare_versions("1.1.0", "v1.1.0") == "same"
    assert doctor_mod.compare_versions("1.1.0", "1.2.0") == "older"
    assert doctor_mod.compare_versions("1.2.0", "1.1.9") == "newer"
    assert doctor_mod.compare_versions("1.1", "1.1.0") == "same"


def test_update_check_skips_network_when_disabled() -> None:
    check = doctor_mod._update_check(enabled=False)

    assert check.status == doctor_mod.OK
    assert "未联网" in check.detail


def test_update_check_reports_newer_release(monkeypatch) -> None:  # noqa: ANN001
    class FakeResponse:
        def read(self) -> bytes:
            return '{"tag_name": "v9.9.9"}'.encode("utf-8")

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResponse())

    check = doctor_mod._update_check()

    assert check.status == doctor_mod.WARN
    assert "9.9.9" in check.detail


def test_update_check_survives_network_error(monkeypatch) -> None:  # noqa: ANN001
    def boom(*_a: object, **_k: object) -> None:
        raise OSError("no network")

    monkeypatch.setattr("urllib.request.urlopen", boom)

    check = doctor_mod._update_check()

    assert check.status == doctor_mod.OK
    assert "已忽略" in check.detail


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
