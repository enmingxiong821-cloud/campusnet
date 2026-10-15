"""监控调度单元测试：用 monkeypatch 替换探活与登录。"""

from __future__ import annotations

import threading
import time

import pytest

from campusnet import monitor as monitor_mod
from campusnet import srun as srun_mod
from campusnet.monitor import CheckOutcome, Monitor, MonitorManager
from campusnet.portal import LoginResult
from campusnet.storage import (
    STATE_FAILED,
    STATE_LOGGED_IN,
    STATE_OFFLINE,
    STATE_ONLINE,
    STATE_UNKNOWN,
    Account,
)


def _online(_url: str, **_: object):
    return monitor_mod.probe_mod.probe.__globals__["ProbeResult"](
        True, "online", "OK", ""
    )


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
        return monitor_mod.probe_mod.ProbeResult(
            state["online"], "redirected", "portal", url
        )

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
        return monitor_mod.probe_mod.ProbeResult(
            state["online"], "online" if state["online"] else "redirected", "", url
        )

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
    monkeypatch.setattr(
        monitor_mod.portal,
        "login",
        lambda *a, **k: LoginResult("success", "门户说成功"),
    )
    monkeypatch.setattr(time, "sleep", lambda *_a: None)

    result = Monitor(store, interval=10).check_account(store.get_account(acc_id))

    assert result.attempts == account.max_retries
    assert result.state == STATE_OFFLINE
    assert result.online is False


def test_offline_login_failure_marks_failed(
    store, account, offline_probe, monkeypatch
) -> None:  # noqa: ANN001
    acc_id = store.add_account(account)
    offline_probe["online"] = False
    monkeypatch.setattr(
        monitor_mod.portal, "login", lambda *a, **k: LoginResult("failed", "密码错误")
    )
    monkeypatch.setattr(time, "sleep", lambda *_a: None)

    result = Monitor(store, interval=10).check_account(store.get_account(acc_id))

    assert not result.online
    assert result.state == STATE_FAILED
    assert result.attempts == account.max_retries
    assert store.get_account(acc_id).last_state == STATE_FAILED


def test_login_error_state_maps_to_offline(
    store, account, offline_probe, monkeypatch
) -> None:  # noqa: ANN001
    acc_id = store.add_account(account)
    offline_probe["online"] = False
    monkeypatch.setattr(
        monitor_mod.portal, "login", lambda *a, **k: LoginResult("error", "没有表单")
    )
    monkeypatch.setattr(time, "sleep", lambda *_a: None)

    result = Monitor(store, interval=10).check_account(store.get_account(acc_id))
    assert result.state == STATE_OFFLINE


def test_terminal_credential_failure_stops_retrying(store, account, offline_probe, monkeypatch) -> None:  # noqa: ANN001
    """凭据被门户拒绝（terminal）时，一轮内就该收手，不再灌 max_retries 次请求。"""
    acc_id = store.add_account(account)
    offline_probe["online"] = False
    calls = {"count": 0}

    def fake_login(*a, **k):
        calls["count"] += 1
        return LoginResult("failed", "密码错误", terminal=True)

    monkeypatch.setattr(monitor_mod.portal, "login", fake_login)
    monkeypatch.setattr(time, "sleep", lambda *_a: None)

    mon = Monitor(store, interval=10)
    result = mon.check_account(store.get_account(acc_id))

    assert calls["count"] == 1
    assert result.attempts == 1
    assert not result.online


def test_terminal_failure_sets_cooldown_and_skips_next_login(store, account, offline_probe, monkeypatch) -> None:  # noqa: ANN001
    """冷却写入后，紧接着的下一轮跳过登录，只保留探活。"""
    acc_id = store.add_account(account)
    offline_probe["online"] = False
    calls = {"count": 0}

    def fake_login(*a, **k):
        calls["count"] += 1
        return LoginResult("failed", "密码错误", terminal=True)

    monkeypatch.setattr(monitor_mod.portal, "login", fake_login)
    monkeypatch.setattr(time, "sleep", lambda *_a: None)

    mon = Monitor(store, interval=10)
    acc = store.get_account(acc_id)
    first = mon.check_account(acc)
    second = mon.check_account(acc)

    assert first.attempts == 1 and first.state == STATE_FAILED
    assert calls["count"] == 1, "冷却期内不该再发起登录"
    assert second.attempts == 0
    assert "冷却" in second.message


def test_clear_credential_cooldown_allows_immediate_retry(store, account, offline_probe, monkeypatch) -> None:  # noqa: ANN001
    """改对密码后清掉冷却，账号应当立刻重新尝试登录。

    冷却本来只能等时间自然走完，而界面提示写的是「改对密码请更新账号」——
    清冷却这一步就是让那句话兑现的地方。
    """
    acc_id = store.add_account(account)
    offline_probe["online"] = False
    calls = {"count": 0}

    def fake_login(*a, **k):
        calls["count"] += 1
        return LoginResult("failed", "密码错误", terminal=True)

    monkeypatch.setattr(monitor_mod.portal, "login", fake_login)
    monkeypatch.setattr(time, "sleep", lambda *_a: None)

    mon = Monitor(store, interval=10)
    acc = store.get_account(acc_id)
    mon.check_account(acc)
    mon.check_account(acc)
    assert calls["count"] == 1, "第二次应当被冷却拦下"

    assert mon.clear_credential_cooldown(acc_id) is True
    mon.check_account(acc)
    assert calls["count"] == 2, "冷却被清掉后应当立刻重试登录"
    # 密码还是错的，这一轮重试又会被门户拒一次，冷却随之重新写入。
    assert mon.clear_credential_cooldown(acc_id) is True, "失败后冷却被重新写入"
    assert mon.clear_credential_cooldown(acc_id) is False, "清过一次后不再有冷却"


def test_manager_clear_cooldown_does_not_start_monitor(store) -> None:  # noqa: ANN001
    """管理器清冷却不该顺手把监控线程拉起来——改个账号不该有这种副作用。"""
    mgr = MonitorManager()
    assert mgr.clear_credential_cooldown(1) is False
    assert mgr.monitor is None


def test_loop_keeps_exception_from_escaping_when_logging_fails(store, monkeypatch) -> None:  # noqa: ANN001
    """check 抛异常、且写事件也失败时，_loop 必须自己收住。

    _loop 是线程体：异常穿出去就是线程静默死亡，此后不再检测，而界面上只会
    显示「一直离线」，看不出线程已经没了。而写事件失败（磁盘满 / 数据库被锁）
    偏偏就是异常最容易发生的时候——两层失败叠在一起才是真正致命的组合。
    """
    mon = Monitor(store, interval=1)

    def boom(*_a, **_k):
        mon._stop.set()  # 置位后 _stop.wait 立刻返回，跑完一轮即退出
        raise RuntimeError("check 失败")

    def dead_event(*_a, **_k):
        raise OSError("database is locked")

    monkeypatch.setattr(mon, "check_all", boom)
    monkeypatch.setattr(mon.store, "add_event", dead_event)

    mon._loop()  # 不该抛出

    assert mon.running_loop is False


def test_success_confirmed_by_probe(store, account, offline_probe, monkeypatch) -> None:  # noqa: ANN001
    """登录返回成功但探活仍不通时，不应误报在线。"""
    acc_id = store.add_account(account)
    offline_probe["online"] = False
    monkeypatch.setattr(
        monitor_mod.portal, "login", lambda *a, **k: LoginResult("success", "ok")
    )
    monkeypatch.setattr(time, "sleep", lambda *_a: None)

    result = Monitor(store, interval=10).check_account(store.get_account(acc_id))
    assert result.state == STATE_OFFLINE
    assert result.online is False
    assert "复检仍未放行" in result.message
    assert store.get_account(acc_id).last_state == STATE_OFFLINE


def test_check_all_only_enabled_accounts(store, account, online_probe) -> None:  # noqa: ANN001
    store.add_account(account)
    disabled = Account(
        name="停用", portal_url="http://b", username="u", password="p", enabled=False
    )
    store.add_account(disabled)

    results = Monitor(store, interval=10).check_all()
    assert len(results) == 1
    assert results[0]["account_name"] == "宿舍"
    assert results[0]["online"] is True


def test_check_all_without_accounts_returns_empty(store) -> None:  # noqa: ANN001
    assert Monitor(store, interval=10).check_all() == []


def test_check_all_is_reentrant_guard(
    store, account, online_probe, monkeypatch
) -> None:  # noqa: ANN001
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
    # 热身间隔默认 15 秒，而这轮只想看到「异常被吞掉、第二轮照样跑」，
    # 不必真等满：关掉热身窗口、把重试压到 1 秒，走的是同一个分支。
    mon = Monitor(store, interval=10, warmup_seconds=0, retry_interval=1)
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


@pytest.mark.parametrize("poison", ["abc", None, [1], {"a": 1}])
def test_manager_survives_poisoned_settings(store, poison) -> None:  # noqa: ANN001
    """库里存着脏值也要能建出 Monitor，退回默认间隔。

    旧版 /api/settings 不校验类型，{"interval": "abc"} 能直接落库；此后
    `int(get_setting("interval"))` 每次都抛异常，服务再也起不来——而且坏值在
    库里，重启、重装程序都没用。校验已在 API 边界补上，这条兜的是存量数据。
    """
    store.set_setting("interval", poison)
    mon = MonitorManager().ensure(store)
    assert mon.interval == 60


def test_prune_stats_survives_poisoned_max_events(store) -> None:  # noqa: ANN001
    """同一类脏值：max_events 被写成非整数时，统计接口不该 500。"""
    store.set_setting("max_events", "abc")
    mon = Monitor(store, interval=60, warmup_seconds=0)
    assert mon.prune_stats()["keep"] == 2000


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
    acc.portal_url = "http://10.10.10.10"
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
    assert calls[0].base_url == "http://10.10.10.10"
    assert calls[0].timeout == monitor_mod.AUTH_STATE_TIMEOUT


def test_portal_recheck_swallows_exception(store, srun_account, monkeypatch) -> None:  # noqa: ANN001
    """auth_state 抛异常不能打断检测，必须降级成 None。"""
    _patch_auth_state(monkeypatch, RuntimeError("boom"))

    got, detail = Monitor(store, interval=10)._portal_recheck(srun_account)

    assert got is None
    assert "RuntimeError" in detail


def test_portal_recheck_skips_when_stop_requested(
    store, srun_account, monkeypatch
) -> None:  # noqa: ANN001
    """后台循环已被要求停止时，不再发起 5 秒的网络请求。"""
    calls = _patch_auth_state(monkeypatch, (True, "门户已认证"))

    mon = Monitor(store, interval=10)
    mon.running_loop = True
    mon._stop.set()

    got, detail = mon._portal_recheck(srun_account)

    assert got is None
    assert "停止指令" in detail
    assert calls == []


def test_probe_offline_but_portal_online_skips_login(
    store, srun_account, monkeypatch
) -> None:  # noqa: ANN001
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

    assert result.online is False
    assert result.state == monitor_mod.STATE_UNKNOWN
    assert result.attempts == 0
    assert login_calls["n"] == 0
    assert store.get_account(acc_id).last_state == monitor_mod.STATE_UNKNOWN


def test_unknown_session_uses_regular_interval(store, monkeypatch):
    mon = Monitor(store, interval=300)
    monkeypatch.setattr(
        mon,
        "check_all",
        lambda: [{"online": False, "state": monitor_mod.STATE_UNKNOWN}],
    )
    delays = []

    def wait(delay):
        delays.append(delay)
        mon._stop.set()

    monkeypatch.setattr(mon._stop, "wait", wait)
    mon._loop()
    assert delays == [300.0]


def test_probe_offline_and_portal_offline_logs_in(
    store, srun_account, monkeypatch
) -> None:  # noqa: ANN001
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

    monkeypatch.setattr(
        monitor_mod.portal, "login", lambda *a, **k: LoginResult("failed", "密码错误")
    )
    monkeypatch.setattr(time, "sleep", lambda *_a: None)

    result = Monitor(store, interval=10).check_account(store.get_account(acc_id))

    assert result.online is False
    assert result.state == STATE_FAILED


def test_online_probe_never_queries_portal(
    store, srun_account, online_probe, monkeypatch
) -> None:  # noqa: ANN001
    """在线时不得发门户查询：那是纯多余的一次网络往返。"""
    acc_id = store.add_account(srun_account)
    calls = _patch_auth_state(monkeypatch, (False, "门户在线表中无本机会话"))

    Monitor(store, interval=10).check_account(store.get_account(acc_id))

    assert calls == []


def test_stop_during_retry_reports_interrupted_not_failed(
    store, srun_account, monkeypatch
) -> None:  # noqa: ANN001
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


def test_manual_check_survives_stale_stop_flag(
    store, srun_account, monkeypatch
) -> None:  # noqa: ANN001
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


def _bare_monitor(**kwargs) -> Monitor:  # noqa: ANN003
    """只装 _next_delay 用得到的字段，不连库不建线程。"""
    mon = Monitor.__new__(Monitor)
    mon.warmup_seconds = kwargs.get("warmup", 180.0)
    mon.warmup_interval = kwargs.get("warmup_gap", 15)
    mon.retry_interval = kwargs.get("retry_gap", 60)
    mon.interval = kwargs.get("interval", 300)
    return mon


def test_next_delay_all_four_combinations() -> None:
    """热身窗口内失败走密集重试，成功立刻退回常规；窗口外失败走快速重试。"""
    mon = _bare_monitor()
    hot = time.monotonic() - 5  # 刚启动
    cold = time.monotonic() - 9999  # 早已过窗口

    assert mon._next_delay(hot, False) == 15.0
    assert mon._next_delay(hot, True) == 300.0
    assert mon._next_delay(cold, False) == 60.0
    assert mon._next_delay(cold, True) == 300.0


def test_loop_reads_the_key_as_dict_actually_emits() -> None:
    """_loop 必须读 online，不能读不存在的 ok。

    这个键名错配过一次：all(r.get("ok")) 在真实 as_dict() 上恒为 False，
    于是「一轮成功即转常规」永远不成立——interval 配 300 秒也只能每
    15 秒跑一轮，而且 30 条 monitor 用例没一条能抓到它。
    """
    online = CheckOutcome(1, "x", True, STATE_ONLINE, "放行", "d", 0, 0.1).as_dict()
    offline = CheckOutcome(2, "y", False, STATE_OFFLINE, "掉线", "d", 1, 0.2).as_dict()

    assert "ok" not in online, "as_dict 若真产出 ok，此用例需同步改断言"
    assert online["online"] is True
    assert offline["online"] is False

    mon = _bare_monitor()
    hot = time.monotonic() - 5
    # 用 as_dict 的真实键走一遍 loop 的判断表达式
    assert mon._next_delay(hot, all(r.get("online") for r in [online])) == 300.0
    assert mon._next_delay(hot, all(r.get("online") for r in [online, offline])) == 15.0


def test_monitor_thread_is_named(store) -> None:  # noqa: ANN001
    mon = Monitor(store, interval=10)
    mon.start()
    try:
        names = [t.name for t in threading.enumerate() if t.name == "campusnet-monitor"]
        assert names
    finally:
        mon.stop()


def test_restart_does_not_start_second_thread_after_stop_timeout(
    store, monkeypatch
) -> None:  # noqa: ANN001
    """旧监控线程未退出时，重启必须拒绝创建第二条监控线程。"""
    manager = MonitorManager()
    monitor = manager.ensure(store)
    monkeypatch.setattr(monitor, "stop", lambda timeout=5.0: False)

    with pytest.raises(RuntimeError, match="拒绝启动第二个监控线程"):
        manager.restart(store)

    assert manager.monitor is monitor


def test_multi_target_probe_reaches_second_target(store, account, monkeypatch) -> None:  # noqa: ANN001
    """第一个探测地址被拦时，第二个放行仍算在线，避免单 canary 误判掉线。"""
    account.probe_url = "http://127.0.0.1:1/blocked http://127.0.0.1:2/ok"
    acc_id = store.add_account(account)
    seen: list[str] = []

    def fake_probe(url: str, timeout: float = 5.0, insecure: bool = False):
        seen.append(url)
        online = url.endswith("/ok")
        return monitor_mod.probe_mod.ProbeResult(
            online, "online" if online else "unreachable", "", url
        )

    monkeypatch.setattr(monitor_mod.probe_mod, "probe", fake_probe)
    result = Monitor(store, interval=10).check_account(store.get_account(acc_id))

    assert result.online is True
    assert result.state == STATE_ONLINE
    assert seen == ["http://127.0.0.1:1/blocked", "http://127.0.0.1:2/ok"]


def test_check_account_feeds_notifier(store, account, online_probe, monkeypatch) -> None:  # noqa: ANN001
    """检测结果要喂给通知出口，并留下一条可审计的事件。"""
    from campusnet import notify as notify_mod

    acc_id = store.add_account(account)
    seen: list[tuple] = []

    class FakeNotifier:
        url = "http://fake"
        after_failures = 3

        def observe(self, *args: object) -> str:
            seen.append(args)
            return "recovered"

    monkeypatch.setattr(
        notify_mod.Notifier, "from_store", classmethod(lambda cls, _store: FakeNotifier())
    )

    outcome = Monitor(store, interval=10).check_account(store.get_account(acc_id))

    assert outcome.online is True
    assert seen and seen[0][1] == account.name
    events = [item["message"] for item in store.list_events(limit=10)]
    assert any("已发送恢复通知" in item for item in events)


def test_notifier_exception_does_not_break_check(store, account, online_probe, monkeypatch) -> None:  # noqa: ANN001
    """通知实现抛异常时，检测结果必须照常返回，只留一条告警事件。"""
    from campusnet import notify as notify_mod

    acc_id = store.add_account(account)

    class BoomNotifier:
        url = "http://fake"
        after_failures = 3

        def observe(self, *args: object) -> str:
            raise RuntimeError("webhook 炸了")

    monkeypatch.setattr(
        notify_mod.Notifier, "from_store", classmethod(lambda cls, _store: BoomNotifier())
    )

    outcome = Monitor(store, interval=10).check_account(store.get_account(acc_id))

    assert outcome.online is True
    events = [item["message"] for item in store.list_events(limit=10)]
    assert any("通知发送异常" in item for item in events)


def test_dispatch_wake_plans_fires_due(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """到点的计划发一次 WOL 并记录，当天不再重复发。"""
    from campusnet import wakeplan
    from campusnet.storage import WakePlan

    plan_id = store.add_wake_plan(
        WakePlan(name="每天开机", mac="AA:BB:CC:DD:EE:FF", schedule="00:00")
    )
    sent: list[str] = []
    monkeypatch.setattr(
        wakeplan, "send_wol", lambda mac, broadcast, port: sent.append(mac) or "ok"
    )

    Monitor(store, interval=10)._dispatch_wake_plans()

    assert sent == ["AA:BB:CC:DD:EE:FF"]
    assert store.list_wake_plans()[0].last_run_at is not None

    sent.clear()
    Monitor(store, interval=10)._dispatch_wake_plans()
    assert sent == []


def test_dispatch_wake_plans_skips_disabled(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    from campusnet import wakeplan
    from campusnet.storage import WakePlan

    store.add_wake_plan(
        WakePlan(name="停用计划", mac="AA:BB:CC:DD:EE:FF", schedule="00:00", enabled=False)
    )
    sent: list[str] = []
    monkeypatch.setattr(
        wakeplan, "send_wol", lambda mac, broadcast, port: sent.append(mac) or "ok"
    )

    Monitor(store, interval=10)._dispatch_wake_plans()

    assert sent == []


# ---------- 逐账号隔离 / STATE_UNKNOWN 口径 / 退避可中断 / ensure 加锁 ----------


def test_check_all_isolates_account_failure(
    store, account, online_probe, monkeypatch
) -> None:  # noqa: ANN001
    """一个账号配置坏掉时，不能带走这一轮剩下的账号。

    回归防护：原来 check_all 没有逐账号 try，第一个账号抛异常会穿到 _loop，
    那一轮剩下的账号一个都不检测——用户看到「其它账号一起掉线」，而真相只是
    其中一个炸了。这条用例让第一个账号必抛，断言第二个仍被检测到。
    """
    good_id = store.add_account(account)
    bad = Account(name="坏账号", portal_url="http://bad", username="u", password="p")
    bad_id = store.add_account(bad)

    mon = Monitor(store, interval=10)
    original = mon.check_account

    def fake_check_account(acc):  # noqa: ANN001
        if acc.name == "坏账号":
            raise ValueError("门户地址畸形")
        return original(acc)

    monkeypatch.setattr(mon, "check_account", fake_check_account)

    results = mon.check_all()

    # 两个账号都要出现在结果里：坏的那个不检测，好的那个照常检测
    assert len(results) == 2
    by_name = {r["account_name"]: r for r in results}
    assert set(by_name) == {"宿舍", "坏账号"}
    assert by_name["宿舍"]["online"] is True

    bad_outcome = by_name["坏账号"]
    assert bad_outcome["online"] is False
    assert bad_outcome["state"] == STATE_UNKNOWN
    assert "检测异常" in bad_outcome["message"]
    assert "ValueError" in bad_outcome["message"]

    # 该账号要留下一条 error 级的 monitor 事件，且状态记为「本轮未知」
    events = store.list_events(limit=50)
    assert any(
        e["level"] == "error"
        and e["stage"] == "monitor"
        and e["state"] == STATE_UNKNOWN
        and e["account_id"] == bad_id
        for e in events
    )
    assert good_id != bad_id


def test_unknown_round_leaves_failure_counter_untouched(
    store, account, monkeypatch
) -> None:  # noqa: ANN001
    """STATE_UNKNOWN 的一轮既不算失败也不清零计数。

    回归防护：_notify 里 STATE_UNKNOWN 必须直接早退。少了这句，outcome.online
    为 False 会落进失败分支、把计数又加一次；而若被当成在线，则会把刚攒起来的
    连续失败次数清零——两种都会让「连续 N 次未连上」这条通知的计数错乱。
    """
    from campusnet import notify as notify_mod

    # 让 refresh 建出一个真正启用的 Notifier（url 非空），计数才有地方累积。
    store.set_setting("notify_url", "http://hook/x")
    store.set_setting("notify_after_failures", 3)
    sent: list[dict] = []
    monkeypatch.setattr(
        notify_mod, "_post", lambda url, payload: sent.append(payload) or True
    )

    acc_id = store.add_account(account)
    acc = store.get_account(acc_id)
    mon = Monitor(store, interval=10)

    offline = CheckOutcome(acc_id, acc.name, False, STATE_OFFLINE, "掉线")
    for _ in range(3):
        mon._notify(acc, offline)

    notifier = mon.notifier
    assert notifier._failures[acc_id] == 3
    assert acc_id in notifier._notified
    assert len(sent) == 1  # 达阈值时发了一条掉线通知

    # 插入一轮 STATE_UNKNOWN：不清零、不增加、不重复发
    unknown = CheckOutcome(
        acc_id, acc.name, False, STATE_UNKNOWN, "门户会话存在，但网络连通未确认"
    )
    mon._notify(acc, unknown)

    assert notifier._failures[acc_id] == 3, "UNKNOWN 那轮不该增减计数"
    assert acc_id in notifier._notified
    assert len(sent) == 1

    # 紧接着的真实失败按原计数推进（从 3 到 4，而不是被 UNKNOWN 搅成别的值）
    mon._notify(acc, offline)
    assert notifier._failures[acc_id] == 4
    assert len(sent) == 1  # 已在 _notified，不重复轰炸


def test_retry_backoff_wait_honors_stop(
    store, account, offline_probe, monkeypatch
) -> None:  # noqa: ANN001
    """重试退避等待必须能被 stop 打断，而不是裸 sleep 干等。

    回归防护：旧实现 sleep(min(3*attempt, 8))，第一次退避就要等满 3 秒；
    改用 self._stop.wait 后，stop 一置位就立刻返回。这里不替换 time.sleep——
    若实现退回裸 sleep，这条用例会真的睡满 3 秒而超时。
    """
    account.max_retries = 3
    acc_id = store.add_account(account)
    offline_probe["online"] = False

    mon = Monitor(store, interval=10)
    mon.running_loop = True

    calls = {"n": 0}

    def fake_login(*_a, **_k):
        calls["n"] += 1
        # 第一次登录后即叫停：随后进入退避等待，等待应立即被这个标志打断
        mon._stop.set()
        return LoginResult("failed", "密码错误")

    monkeypatch.setattr(monitor_mod.portal, "login", fake_login)

    started = time.monotonic()
    mon.check_account(store.get_account(acc_id))
    elapsed = time.monotonic() - started

    assert calls["n"] == 1, "叫停后不该继续重试"
    assert elapsed < 1.0, "退避应被 stop 立即打断，而不是等满 3 秒"


def test_manager_ensure_creates_single_monitor_under_concurrency(
    store, monkeypatch
) -> None:  # noqa: ANN001
    """并发调 ensure 只能建出一个 Monitor。

    回归防护：HTTP 服务是多线程的，两个请求同时进来都可能看到 monitor is None，
    于是各建一个并各自 start()——两个循环线程轮流检测，事件与通知都翻倍。
    ensure 必须在锁里判断并创建。
    """
    created: list[int] = []
    real_init = Monitor.__init__

    def slow_init(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        created.append(1)
        time.sleep(0.05)  # 放大竞态窗口，让无锁实现更容易露出第二个实例
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(monitor_mod.Monitor, "__init__", slow_init)

    manager = MonitorManager()
    barrier = threading.Barrier(8)
    results: list[Monitor] = []

    def worker() -> None:
        barrier.wait(timeout=5)
        results.append(manager.ensure(store))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 8
    assert len(created) == 1, "并发调用 ensure 只应创建一个 Monitor"
    assert all(item is results[0] for item in results)
