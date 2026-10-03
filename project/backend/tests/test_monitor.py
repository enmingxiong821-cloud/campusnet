"""监控调度单元测试：用 monkeypatch 替换探活与登录。"""

from __future__ import annotations

import threading
import time

import pytest

from campusnet import monitor as monitor_mod
from campusnet.monitor import CheckOutcome, Monitor, MonitorManager
from campusnet.portal import LoginResult
from campusnet.storage import STATE_FAILED, STATE_LOGGED_IN, STATE_OFFLINE, STATE_ONLINE, Account


def _online(_url: str, **_: object):
    return monitor_mod.probe_mod.probe.__globals__["ProbeResult"](True, "online", "OK", "")


@pytest.fixture()
def online_probe(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake_probe(url: str, timeout: float = 5.0, insecure: bool = False):
        calls.append(url)
        return monitor_mod.probe_mod.ProbeResult(True, "online", "ok", url)

    monkeypatch.setattr(monitor_mod.probe_mod, "probe", fake_probe)
    return calls


@pytest.fixture()
def offline_probe(monkeypatch: pytest.MonkeyPatch):
    state = {"online": False}

    def fake_probe(url: str, timeout: float = 5.0, insecure: bool = False):
        return monitor_mod.probe_mod.ProbeResult(state["online"], "redirected", "portal", url)

    monkeypatch.setattr(monitor_mod.probe_mod, "probe", fake_probe)
    return state


def test_online_account_skips_login(store, account, online_probe, monkeypatch) -> None:  # noqa: ANN001
    acc_id = store.add_account(account)
    called = {"login": 0}

    def fake_login(*_a, **_k):
        called["login"] += 1
        return LoginResult("success", "")

    monkeypatch.setattr(monitor_mod.portal, "login", fake_login)

    result = Monitor(store, interval=10).check_account(store.get_account(acc_id))

    assert result.online and result.state == STATE_ONLINE
    assert result.attempts == 0
    assert called["login"] == 0
    assert store.get_account(acc_id).last_state == STATE_ONLINE


def test_offline_then_login_success(store, account, monkeypatch) -> None:  # noqa: ANN001
    """登录后探活放行，应只尝试一次并记为已登录。"""
    acc_id = store.add_account(account)
    state = {"online": False}

    def fake_probe(url: str, timeout: float = 5.0, insecure: bool = False):
        return monitor_mod.probe_mod.ProbeResult(state["online"], "online" if state["online"] else "redirected", "", url)

    monkeypatch.setattr(monitor_mod.probe_mod, "probe", fake_probe)

    def fake_login(*_a, **_k):
        state["online"] = True
        return LoginResult("success", "门户放行")

    monkeypatch.setattr(monitor_mod.portal, "login", fake_login)
    monkeypatch.setattr(time, "sleep", lambda *_a: None)

    result = Monitor(store, interval=10).check_account(store.get_account(acc_id))

    assert result.online and result.state == STATE_LOGGED_IN
    assert result.attempts == 1
    assert store.get_account(acc_id).last_state == STATE_LOGGED_IN


def test_portal_ok_but_probe_still_blocked_retries(store, account, monkeypatch) -> None:  # noqa: ANN001
    """门户报成功但探活一直不通时，必须重试满次数而不是当成已上线。"""
    acc_id = store.add_account(account)

    def fake_probe(url: str, timeout: float = 5.0, insecure: bool = False):
        return monitor_mod.probe_mod.ProbeResult(False, "redirected", "", url)

    monkeypatch.setattr(monitor_mod.probe_mod, "probe", fake_probe)
    monkeypatch.setattr(monitor_mod.portal, "login", lambda *a, **k: LoginResult("success", "门户说成功"))
    monkeypatch.setattr(time, "sleep", lambda *_a: None)

    result = Monitor(store, interval=10).check_account(store.get_account(acc_id))

    assert result.attempts == account.max_retries
    assert result.state == STATE_LOGGED_IN


def test_offline_login_failure_marks_failed(store, account, offline_probe, monkeypatch) -> None:  # noqa: ANN001
    acc_id = store.add_account(account)
    offline_probe["online"] = False
    monkeypatch.setattr(monitor_mod.portal, "login", lambda *a, **k: LoginResult("failed", "密码错误"))
    monkeypatch.setattr(time, "sleep", lambda *_a: None)

    result = Monitor(store, interval=10).check_account(store.get_account(acc_id))

    assert not result.online
    assert result.state == STATE_FAILED
    assert result.attempts == account.max_retries
    assert store.get_account(acc_id).last_state == STATE_FAILED


def test_login_error_state_maps_to_offline(store, account, offline_probe, monkeypatch) -> None:  # noqa: ANN001
    acc_id = store.add_account(account)
    offline_probe["online"] = False
    monkeypatch.setattr(monitor_mod.portal, "login", lambda *a, **k: LoginResult("error", "没有表单"))
    monkeypatch.setattr(time, "sleep", lambda *_a: None)

    result = Monitor(store, interval=10).check_account(store.get_account(acc_id))
    assert result.state == STATE_OFFLINE


def test_success_confirmed_by_probe(store, account, offline_probe, monkeypatch) -> None:  # noqa: ANN001
    """登录返回成功但探活仍不通时，不应误报在线。"""
    acc_id = store.add_account(account)
    offline_probe["online"] = False
    monkeypatch.setattr(monitor_mod.portal, "login", lambda *a, **k: LoginResult("success", "ok"))
    monkeypatch.setattr(time, "sleep", lambda *_a: None)

    result = Monitor(store, interval=10).check_account(store.get_account(acc_id))
    assert result.state == STATE_LOGGED_IN  # 门户确认成功即记为已登录
    assert result.online is True


def test_check_all_only_enabled_accounts(store, account, online_probe) -> None:  # noqa: ANN001
    store.add_account(account)
    disabled = Account(name="停用", portal_url="http://b", username="u", password="p", enabled=False)
    store.add_account(disabled)

    results = Monitor(store, interval=10).check_all()
    assert len(results) == 1
    assert results[0]["account_name"] == "宿舍"
    assert results[0]["online"] is True


def test_check_all_without_accounts_returns_empty(store) -> None:  # noqa: ANN001
    assert Monitor(store, interval=10).check_all() == []


def test_check_all_is_reentrant_guard(store, account, online_probe, monkeypatch) -> None:  # noqa: ANN001
    store.add_account(account)
    mon = Monitor(store, interval=10)
    reentered = {"count": 0}

    original = mon.check_account

    def slow(account_obj):  # noqa: ANN001
        reentered["count"] += 1
        if reentered["count"] == 1:
            assert mon.check_all() == []  # 持锁期间应直接返回上次结果
        return original(account_obj)

    monkeypatch.setattr(mon, "check_account", slow)
    mon.check_all()
    assert reentered["count"] == 1


def test_interval_is_clamped() -> None:
    assert Monitor._clamp(1) == 10
    assert Monitor._clamp(99999) == 3600
    assert Monitor._clamp(120) == 120


def test_start_and_stop_thread(store, account, online_probe) -> None:  # noqa: ANN001
    store.add_account(account)
    mon = Monitor(store, interval=10)
    assert mon.start() is True
    assert mon.is_alive() is True
    assert mon.start() is False  # 已在运行
    assert mon.stop() is True
    assert mon.is_alive() is False


def test_status_payload(store) -> None:  # noqa: ANN001
    status = Monitor(store, interval=45).status()
    assert status["alive"] is False
    assert status["interval"] == 45
    assert status["last_results"] == []


def test_loop_survives_exception(store, account, monkeypatch) -> None:  # noqa: ANN001
    """单轮 check_all 抛异常时，异常要落进事件表而不是静默丢失。"""
    store.add_account(account)
    mon = Monitor(store, interval=10)
    boom = {"n": 0}

    def exploding_check_all():
        boom["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(mon, "check_all", exploding_check_all)

    for _ in range(2):
        try:
            mon.check_all()
        except RuntimeError:
            mon.store.add_event("监控循环异常: boom", level="error", stage="monitor")

    assert boom["n"] == 2
    assert any("监控循环异常" in e["message"] for e in store.list_events(limit=50))


def test_loop_catches_exception_from_iteration(store, account, monkeypatch) -> None:  # noqa: ANN001
    """直接驱动 _loop，验证它自己吞掉异常后继续下一轮。"""
    store.add_account(account)
    mon = Monitor(store, interval=10)
    calls = {"n": 0}

    def check_all_then_stop():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        mon._stop.set()  # 第二轮跑完就收工
        return []

    monkeypatch.setattr(mon, "check_all", check_all_then_stop)
    mon._loop()

    assert calls["n"] == 2
    assert any("监控循环异常" in e["message"] for e in store.list_events(limit=50))


def test_manager_restart_applies_interval(store) -> None:  # noqa: ANN001
    mgr = MonitorManager()
    mon = mgr.start(store)
    assert mon.is_alive()
    mgr.restart(store, interval=200)
    current = mgr.monitor
    assert current is not None
    assert current.interval == 200
    assert store.get_setting("interval") == 200
    mgr.stop()


def test_manager_ensure_is_idempotent(store) -> None:  # noqa: ANN001
    mgr = MonitorManager()
    first = mgr.ensure(store)
    assert mgr.ensure(store) is first
    mgr.stop()


def test_check_outcome_as_dict_shape() -> None:
    out = CheckOutcome(1, "宿舍", True, "online", "ok", "d", 2, 1.2345).as_dict()
    assert out["elapsed"] == 1.234 or out["elapsed"] == 1.235
    assert out["attempts"] == 2
    assert set(out) == {
        "account_id",
        "account_name",
        "online",
        "state",
        "message",
        "probe_detail",
        "attempts",
        "elapsed",
    }


def test_monitor_thread_is_named(store) -> None:  # noqa: ANN001
    mon = Monitor(store, interval=10)
    mon.start()
    try:
        names = [t.name for t in threading.enumerate() if t.name == "campusnet-monitor"]
        assert names
    finally:
        mon.stop()