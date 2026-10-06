"""监控调度单元测试：用 monkeypatch 替换探活与登录。"""

from __future__ import annotations

import threading
import time

import pytest

from campusnet import monitor as monitor_mod
from campusnet import srun as srun_mod
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


# ---------- 门户会话复核（srun auth_state 接入） ----------


@pytest.fixture()
def srun_account(account):  # noqa: ANN001
    acc = account
    acc.portal_url = "http://172.26.8.11"
    acc.login_mode = "srun"
    return acc


def _patch_auth_state(monkeypatch: pytest.MonkeyPatch, result) -> list:  # noqa: ANN001
    """替换 srun.auth_state，记录每次调用的 config。"""
    calls: list = []

    def fake_auth_state(config):  # noqa: ANN001
        calls.append(config)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(srun_mod, "auth_state", fake_auth_state)
    return calls


def _patch_probe_offline(monkeypatch: pytest.MonkeyPatch, detail: str = "") -> None:  # noqa: ANN001
    monkeypatch.setattr(
        monitor_mod.probe_mod,
        "probe",
        lambda url, timeout=5.0, insecure=False: monitor_mod.probe_mod.ProbeResult(
            False, "redirected", detail, url
        ),
    )


def test_portal_recheck_short_circuits_non_srun(store, account, monkeypatch) -> None:  # noqa: ANN001
    """非 SRUN 门户必须短路，绝不能为它白打一次 rad_user_info。"""
    account.portal_url = "http://10.0.0.1/eportal/portal/login"  # Dr.COM 风格
    account.login_mode = "http"
    calls = _patch_auth_state(monkeypatch, (True, "x"))

    got, detail = Monitor(store, interval=10)._portal_recheck(account)

    assert got is None
    assert "非 SRUN" in detail
    assert calls == []


def test_portal_recheck_returns_none_on_empty_url(store, account) -> None:  # noqa: ANN001
    account.portal_url = "   "
    got, detail = Monitor(store, interval=10)._portal_recheck(account)
    assert got is None
    assert "未配置门户地址" in detail


def test_portal_recheck_propagates_auth_state(store, srun_account, monkeypatch) -> None:  # noqa: ANN001
    """SRUN 门户才发请求，且三态原样透传。"""
    calls = _patch_auth_state(monkeypatch, (True, "门户已认证（20240001）"))

    got, detail = Monitor(store, interval=10)._portal_recheck(srun_account)

    assert got is True
    assert "门户已认证" in detail
    assert len(calls) == 1
    assert calls[0].base_url == "http://172.26.8.11"
    assert calls[0].timeout == monitor_mod.AUTH_STATE_TIMEOUT


def test_portal_recheck_swallows_exception(store, srun_account, monkeypatch) -> None:  # noqa: ANN001
    """auth_state 抛异常不能打断检测，必须降级成 None。"""
    _patch_auth_state(monkeypatch, RuntimeError("boom"))

    got, detail = Monitor(store, interval=10)._portal_recheck(srun_account)

    assert got is None
    assert "RuntimeError" in detail


def test_portal_recheck_skips_when_stop_requested(store, srun_account, monkeypatch) -> None:  # noqa: ANN001
    """后台循环已被要求停止时，不再发起 5 秒的网络请求。"""
    calls = _patch_auth_state(monkeypatch, (True, "门户已认证"))

    mon = Monitor(store, interval=10)
    mon.running_loop = True
    mon._stop.set()

    got, detail = mon._portal_recheck(srun_account)

    assert got is None
    assert "停止指令" in detail
    assert calls == []


def test_probe_offline_but_portal_online_skips_login(store, srun_account, monkeypatch) -> None:  # noqa: ANN001
    """假掉线：canary 挂了但门户说有会话 —— 不许重登。"""
    acc_id = store.add_account(srun_account)
    _patch_auth_state(monkeypatch, (True, "门户已认证（20240001）"))
    _patch_probe_offline(monkeypatch, "探测超时")

    login_calls = {"n": 0}

    def fake_login(*_a, **_k):
        login_calls["n"] += 1
        return LoginResult("success", "不该被调用")

    monkeypatch.setattr(monitor_mod.portal, "login", fake_login)

    result = Monitor(store, interval=10).check_account(store.get_account(acc_id))

    assert result.online is True
    assert result.state == STATE_ONLINE
    assert result.attempts == 0
    assert login_calls["n"] == 0
    assert store.get_account(acc_id).last_state == STATE_ONLINE


def test_probe_offline_and_portal_offline_logs_in(store, srun_account, monkeypatch) -> None:  # noqa: ANN001
    """真掉线：门户也说没会话 —— 必须照常走登录。"""
    acc_id = store.add_account(srun_account)
    _patch_auth_state(monkeypatch, (False, "门户在线表中无本机会话"))

    state = {"online": False}

    def fake_probe(url: str, timeout: float = 5.0, insecure: bool = False):
        return monitor_mod.probe_mod.ProbeResult(state["online"], "online", "", url)

    monkeypatch.setattr(monitor_mod.probe_mod, "probe", fake_probe)

    def fake_login(*_a, **_k):
        state["online"] = True
        return LoginResult("success", "门户放行")

    monkeypatch.setattr(monitor_mod.portal, "login", fake_login)
    monkeypatch.setattr(time, "sleep", lambda *_a: None)

    result = Monitor(store, interval=10).check_account(store.get_account(acc_id))

    assert result.state == STATE_LOGGED_IN
    assert result.attempts == 1


def test_portal_unknown_falls_back_to_probe(store, srun_account, monkeypatch) -> None:  # noqa: ANN001
    """问不出状态时必须按探活结论走，不能当在线也不能当掉线之外的第三种。"""
    acc_id = store.add_account(srun_account)
    _patch_auth_state(monkeypatch, (None, "门户响应无法判读（无 res/error 字段）"))
    _patch_probe_offline(monkeypatch)

    monkeypatch.setattr(monitor_mod.portal, "login", lambda *a, **k: LoginResult("failed", "密码错误"))
    monkeypatch.setattr(time, "sleep", lambda *_a: None)

    result = Monitor(store, interval=10).check_account(store.get_account(acc_id))

    assert result.online is False
    assert result.state == STATE_FAILED


def test_online_probe_never_queries_portal(store, srun_account, online_probe, monkeypatch) -> None:  # noqa: ANN001
    """在线时不得发门户查询：那是纯多余的一次网络往返。"""
    acc_id = store.add_account(srun_account)
    calls = _patch_auth_state(monkeypatch, (False, "门户在线表中无本机会话"))

    Monitor(store, interval=10).check_account(store.get_account(acc_id))

    assert calls == []


def test_stop_during_retry_reports_interrupted_not_failed(store, srun_account, monkeypatch) -> None:  # noqa: ANN001
    """后台循环里收到停止指令，必须报「已中断」，不能报成「重连未成功」。

    回归防护：中断曾经落在 failed 分支上，于是
      1. 事件日志多一条 error 级「重连未成功 - 未执行登录」的假错误，
      2. 状态置成 failed，前端把 offline/failed 一起算进「需要关注」，
         用户主动停一次监控，账号就凭空多一个红标。
    """
    acc_id = store.add_account(srun_account)
    _patch_probe_offline(monkeypatch)
    _patch_auth_state(monkeypatch, (False, "门户在线表中无本机会话"))
    monkeypatch.setattr(
        monitor_mod.portal, "login", lambda *a, **k: LoginResult("failed", "不该被调用")
    )

    mon = Monitor(store, interval=10)
    mon.running_loop = True
    mon._stop.set()  # 在进重试循环前就叫停

    result = mon.check_account(store.get_account(acc_id))

    assert result.state == STATE_OFFLINE
    assert result.online is False
    assert result.attempts == 0
    assert "中断" in result.message

    events = store.list_events(limit=50)
    assert not [e for e in events if e["level"] == "error"], "中断不应产生 error 级事件"
    assert any("中断" in e["message"] for e in events)
    assert not any("重连未成功" in e["message"] for e in events), "中断不该报成重连失败"
    assert store.get_account(acc_id).last_state == STATE_OFFLINE


def test_abort_stops_further_retries(store, srun_account, monkeypatch) -> None:  # noqa: ANN001
    """中断发生在重试途中同样不能报成 failed，也不该继续重试。"""
    srun_account.max_retries = 3
    acc_id = store.add_account(srun_account)
    _patch_probe_offline(monkeypatch)
    _patch_auth_state(monkeypatch, (False, "门户在线表中无本机会话"))

    mon = Monitor(store, interval=10)
    mon.running_loop = True

    calls = {"n": 0}

    def fake_login(*_a, **_k):
        calls["n"] += 1
        mon._stop.set()  # 第一次登录后即叫停
        return LoginResult("failed", "密码错误")

    monkeypatch.setattr(monitor_mod.portal, "login", fake_login)
    monkeypatch.setattr(time, "sleep", lambda *_a: None)

    result = mon.check_account(store.get_account(acc_id))

    assert calls["n"] == 1, "叫停后不该继续重试"
    assert result.state == STATE_OFFLINE
    assert result.attempts == 1
    assert not [e for e in store.list_events(limit=50) if e["level"] == "error"]


def test_manual_check_survives_stale_stop_flag(store, srun_account, monkeypatch) -> None:  # noqa: ANN001
    """手动「立即检测」时监控线程没在跑，残留的 _stop 不能中断它。

    这是 running_loop 存在的原因：手动与后台共用同一条路径，
    若不看这个标志，用户点一次检测就可能被自己的 stop 打断成假失败。
    """
    acc_id = store.add_account(srun_account)
    _patch_auth_state(monkeypatch, (False, "门户在线表中无本机会话"))

    state = {"online": False}

    def fake_probe(url: str, timeout: float = 5.0, insecure: bool = False):
        return monitor_mod.probe_mod.ProbeResult(state["online"], "online", "", url)

    monkeypatch.setattr(monitor_mod.probe_mod, "probe", fake_probe)

    def fake_login(*_a, **_k):
        state["online"] = True
        return LoginResult("success", "门户放行")

    monkeypatch.setattr(monitor_mod.portal, "login", fake_login)

    mon = Monitor(store, interval=10)
    mon._stop.set()  # 之前停过一次，标志还留着
    mon.running_loop = False

    result = mon.check_account(store.get_account(acc_id))

    assert result.state == STATE_LOGGED_IN
    assert result.attempts == 1


def test_monitor_thread_is_named(store) -> None:  # noqa: ANN001
    mon = Monitor(store, interval=10)
    mon.start()
    try:
        names = [t.name for t in threading.enumerate() if t.name == "campusnet-monitor"]
        assert names
    finally:
        mon.stop()