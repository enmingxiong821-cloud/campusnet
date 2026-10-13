"""稳定性测试：事件裁剪、单实例锁、跨进程互斥。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from campusnet.monitor import DEFAULT_MAX_EVENTS, PRUNE_STATS_KEY, Monitor
from campusnet.storage import Store


@pytest.fixture()
def online_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """让探活一律返回在线，避免测试碰真实网络。"""
    from campusnet import monitor as monitor_mod

    def fake_probe(url: str, timeout: float = 5.0, insecure: bool = False):
        return monitor_mod.probe_mod.ProbeResult(True, "online", "ok", url)

    monkeypatch.setattr(monitor_mod.probe_mod, "probe", fake_probe)


# ---------- 事件裁剪 ----------


def test_prune_events_keeps_latest(store: Store) -> None:
    for i in range(50):
        store.add_event(f"第 {i} 条")
    deleted = store.prune_events(keep=10)

    assert deleted == 40
    events = store.list_events(limit=100)
    assert len(events) == 10
    assert events[0]["message"] == "第 49 条"


def test_prune_events_is_noop_below_limit(store: Store) -> None:
    for i in range(5):
        store.add_event(f"e{i}")
    assert store.prune_events(keep=100) == 0
    assert store.event_count() == 5


def test_prune_events_min_keep_is_one(store: Store) -> None:
    for i in range(10):
        store.add_event(f"e{i}")
    store.prune_events(keep=0)
    assert store.event_count() == 1


def test_prune_events_rejects_negative(store: Store) -> None:
    store.add_event("x")
    store.prune_events(keep=-5)
    assert store.event_count() == 1


def test_prune_events_on_empty_table(store: Store) -> None:
    assert store.prune_events(keep=10) == 0


def test_event_count_tracks_writes(store: Store) -> None:
    assert store.event_count() == 0
    store.add_event("a")
    store.add_event("b")
    assert store.event_count() == 2
    store.clear_events()
    assert store.event_count() == 0


def test_monitor_prunes_after_each_round(store, account, online_probe) -> None:  # noqa: ANN001
    """一轮检查后应自动裁剪，而不是让事件表无限膨胀。"""
    store.add_account(account)
    store.set_setting("max_events", 5)
    for i in range(30):
        store.add_event(f"历史 {i}")

    Monitor(store, interval=10).check_all()

    # 裁剪后只剩最近 5 条，加上「已裁剪 N 条」这条日志本身
    assert store.event_count() <= 6


def test_prune_announces_only_once(store, account, online_probe) -> None:  # noqa: ANN001
    """裁剪公告不能每轮都写。

    每轮补一条 maintenance 记录的话，它自己就成了永远保留的那条，
    长跑下来 2000 条里塞满裁剪公告（实测 341 条）。

    注意断言的是「不刷屏」而非「永远留存」：公告本身也是历史事件，
    keep=5 时它照样会被后续轮次裁掉，这符合预期。
    """
    store.add_account(account)
    store.set_setting("max_events", 5)
    mon = Monitor(store, interval=10)

    max_maintenance = 0
    for _ in range(30):
        for i in range(10):
            store.add_event(f"噪声 {i}")
        mon.check_all()
        count = len([e for e in store.list_events(limit=500) if e["stage"] == "maintenance"])
        max_maintenance = max(max_maintenance, count)

    assert max_maintenance <= 1, f"30 轮内公告最多出现 1 次，实际 {max_maintenance} 次"
    assert mon._prune_announced is True
    # 首次裁剪时确实记过一次公告
    assert store.event_count() <= 6


def test_prune_stats_accumulate_in_settings(store, account, online_probe) -> None:  # noqa: ANN001
    """裁剪统计落在 settings，不放 events。

    公告写进 events 会被下一轮裁掉，于是「计数器认为已记录、
    实际记录已消失」，长跑下来公告永远不再出现。
    """
    store.add_account(account)
    store.set_setting("max_events", 5)
    mon = Monitor(store, interval=10)

    for _ in range(20):
        for i in range(10):
            store.add_event(f"噪声 {i}")
        mon.check_all()

    stats = mon.prune_stats()
    assert stats["total_removed"] > 0
    assert stats["keep"] == 5
    assert stats["last_at"] is not None
    # 统计写在 settings 里，因此不会被裁剪掉
    assert store.get_setting(PRUNE_STATS_KEY) is not None


def test_prune_stats_persist_across_monitor_restart(store, account, online_probe) -> None:  # noqa: ANN001
    """重启Monitor 后累计统计仍在，且不会重复发公告。"""
    store.add_account(account)
    store.set_setting("max_events", 5)

    for _ in range(10):
        for i in range(10):
            store.add_event(f"噪声 {i}")
        Monitor(store, interval=10).check_all()

    first_total = Monitor(store, interval=10).prune_stats()["total_removed"]
    assert first_total > 0

    for _ in range(10):
        for i in range(10):
            store.add_event(f"噪声 {i}")
        Monitor(store, interval=10).check_all()

    second = Monitor(store, interval=10).prune_stats()
    assert second["total_removed"] > first_total
    maintenance = [e for e in store.list_events(limit=2000) if e["stage"] == "maintenance"]
    assert len(maintenance) == 1


def test_prune_stats_survive_event_pruning(store, account, online_probe) -> None:  # noqa: ANN001
    """无论事件怎么裁，统计都不会丢。"""
    store.add_account(account)
    store.set_setting("max_events", 3)
    mon = Monitor(store, interval=10)

    for _ in range(40):
        for i in range(10):
            store.add_event(f"噪声 {i}")
        mon.check_all()

    assert store.event_count() <= 4
    assert mon.prune_stats()["total_removed"] > 0
    assert store.get_setting(PRUNE_STATS_KEY)["keep"] == 3


def test_prune_not_written_when_nothing_removed(store, account, online_probe) -> None:  # noqa: ANN001
    """没裁掉东西时不该产生任何裁剪记录。"""
    store.add_account(account)
    store.set_setting("max_events", 1000)
    mon = Monitor(store, interval=10)

    for _ in range(5):
        mon.check_all()

    maintenance = [e for e in store.list_events(limit=500) if e["stage"] == "maintenance"]
    assert maintenance == []
    assert mon.prune_stats()["total_removed"] == 0
    assert mon.prune_stats()["last_at"] is None


def test_prune_stats_default_shape_when_never_run(store) -> None:  # noqa: ANN001
    """没裁过时统计也有完整结构，前端不会拿到 None。"""
    stats = Monitor(store, interval=10).prune_stats()
    assert set(stats) >= {"last_removed", "last_at", "total_removed", "keep", "suppressed"}
    assert stats["keep"] == DEFAULT_MAX_EVENTS


def test_prune_stats_key_is_constant() -> None:
    """统计的 settings 键名不能随手改，否则读不到历史统计。"""
    assert PRUNE_STATS_KEY == "prune_stats"


def test_prune_log_interval_rejects_negative(store) -> None:  # noqa: ANN001
    """负数间隔会被夹到 0，不会让限频逻辑失效。"""
    mon = Monitor(store, interval=10, prune_log_interval=-99.0)
    assert mon.prune_log_interval == 0.0


def test_monitor_prune_failure_does_not_break_results(
    store, account, online_probe, monkeypatch
) -> None:  # noqa: ANN001
    """裁剪是维护任务，失败不该让检测结果丢失。"""
    store.add_account(account)

    def boom(*_a, **_k):
        raise RuntimeError("prune down")

    monkeypatch.setattr(store, "prune_events", boom)

    results = Monitor(store, interval=10).check_all()

    assert len(results) == 1 and results[0]["online"] is True
    assert any("事件裁剪失败" in e["message"] for e in store.list_events(limit=50))


def test_default_max_events_is_sane() -> None:
    assert 100 <= DEFAULT_MAX_EVENTS <= 100_000


# ---------- 单实例锁 ----------


def test_lock_acquire_and_release(tmp_path: Path) -> None:
    from campusnet.lockfile import SingleInstance

    lock = SingleInstance(tmp_path / "cn.lock")
    assert lock.acquire() is True
    assert lock.holder_pid() == os.getpid()
    lock.release()
    assert lock.path.exists() is False


def test_lock_repeat_acquire_is_idempotent(tmp_path: Path) -> None:
    """同一实例重复 acquire 仍是 True，且 release 能真正清掉。"""
    from campusnet.lockfile import SingleInstance

    lock = SingleInstance(tmp_path / "cn.lock")
    assert lock.acquire() is True
    assert lock.acquire() is True
    lock.release()
    assert lock.path.exists() is False


def test_lock_release_without_acquire_is_safe(tmp_path: Path) -> None:
    from campusnet.lockfile import SingleInstance

    SingleInstance(tmp_path / "cn.lock").release()  # 不应抛异常


def test_lock_ignores_dead_holder(tmp_path: Path) -> None:
    """上次崩溃留下的锁不能让服务永远起不来。"""
    from campusnet.lockfile import SingleInstance

    lock_path = tmp_path / "cn.lock"
    lock_path.write_text("999999", encoding="utf-8")

    lock = SingleInstance(lock_path)
    assert lock.is_held_by_other_process() is False
    assert lock.acquire() is True
    lock.release()


def test_lock_reports_live_foreign_holder(tmp_path: Path) -> None:
    """别的活进程真的持锁时要认出来。

    判定以内核文件锁为准，所以必须让子进程**真的**去 acquire，
    光在文件里写一个活着的 PID 不再算「持有」——那正是会被 PID 复用坑到的判法，
    见 test_lock_ignores_stale_file_with_reused_pid。
    """
    from campusnet.lockfile import SingleInstance

    backend = Path(__file__).resolve().parents[1]
    lock_path = tmp_path / "cn.lock"
    proc = subprocess.Popen(
        [sys.executable, "-c", _HELPER, str(lock_path), str(backend)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ACQUIRED"
        lock = SingleInstance(lock_path)
        assert lock.is_held_by_other_process() is True
        assert lock.acquire() is False
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_lock_ignores_stale_file_with_reused_pid(tmp_path: Path) -> None:
    """残留锁文件里是个活着的无关进程，也不能挡住启动。

    系统会把 PID 复用给别的程序（我们的文件里那个 PID 已经属于无关进程了），
    旧实现按 PID 判活就会把服务永久挡在门外，只能靠人工删文件。
    """
    from campusnet.lockfile import SingleInstance

    helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lock_path = tmp_path / "cn.lock"
        lock_path.write_text(str(helper.pid), encoding="utf-8")
        lock = SingleInstance(lock_path)
        assert lock.is_held_by_other_process() is False
        assert lock.acquire() is True
        assert lock.holder_pid() == os.getpid()
        lock.release()
    finally:
        helper.terminate()
        helper.wait(timeout=10)


def test_lock_rejects_garbage_holder_file(tmp_path: Path) -> None:
    from campusnet.lockfile import SingleInstance

    lock_path = tmp_path / "cn.lock"
    lock_path.write_text("这不是 PID", encoding="utf-8")

    lock = SingleInstance(lock_path)
    assert lock.holder_pid() is None
    assert lock.acquire() is True
    lock.release()


def test_lock_works_as_context_manager(tmp_path: Path) -> None:
    from campusnet.lockfile import SingleInstance

    lock_path = tmp_path / "cn.lock"
    with SingleInstance(lock_path) as lock:
        assert lock.acquired is True
        assert lock_path.exists()
    assert lock_path.exists() is False


# ---------- 跨进程互斥（真实验证） ----------


_HELPER = """
import sys, time
sys.path.insert(0, sys.argv[2])
from campusnet.lockfile import SingleInstance
lock = SingleInstance(sys.argv[1])
print('ACQUIRED' if lock.acquire() else 'BLOCKED', flush=True)
time.sleep(3)
lock.release()
"""


def _run_helper(lock_path: Path, backend: Path) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", _HELPER, str(lock_path), str(backend)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.stdout.strip()


def test_second_process_is_blocked_while_first_holds_lock(tmp_path: Path) -> None:
    """真起一个子进程持锁，主进程必须拿不到 —— 这才是互斥要挡的场景。"""
    from campusnet.lockfile import SingleInstance

    backend = Path(__file__).resolve().parents[1]
    lock_path = tmp_path / "cn.lock"

    mine = SingleInstance(lock_path)
    assert mine.acquire() is True

    # 另一个进程看到本进程 PID 的锁，应当被挡
    proc = subprocess.Popen(
        [sys.executable, "-c", _HELPER, str(lock_path), str(backend)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = proc.stdout.readline().strip()
        assert line == "BLOCKED", f"子进程应被挡住，实际输出 {line!r}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    mine.release()

    # 释放后子进程就能拿到
    assert _run_helper(lock_path, backend).startswith("ACQUIRED")


def test_lock_survives_holder_process_exit(tmp_path: Path) -> None:
    """持锁进程被杀掉后，锁必须能被接管。"""
    backend = Path(__file__).resolve().parents[1]
    lock_path = tmp_path / "cn.lock"

    proc = subprocess.Popen(
        [sys.executable, "-c", _HELPER, str(lock_path), str(backend)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "ACQUIRED"
    proc.kill()
    proc.wait(timeout=10)

    from campusnet.lockfile import SingleInstance

    lock = SingleInstance(lock_path)
    assert lock.acquire() is True
    lock.release()