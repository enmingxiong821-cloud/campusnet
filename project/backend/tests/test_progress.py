"""实时进度通道测试：阶段是否按时吐出、seq 是否可增量拉取。"""

from __future__ import annotations

import pytest

from campusnet.monitor import DEFAULT_PROGRESS_CAP, Monitor, ProgressItem
from campusnet.storage import Account


@pytest.fixture()
def online_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    from campusnet import monitor as monitor_mod

    def fake_probe(url: str, timeout: float = 5.0, insecure: bool = False):
        return monitor_mod.probe_mod.ProbeResult(True, "online", "ok", url)

    monkeypatch.setattr(monitor_mod.probe_mod, "probe", fake_probe)


# ---------- ProgressItem ----------


def test_progress_item_as_dict_shape() -> None:
    item = ProgressItem(1, "宿舍", "probe", "正在探测", 123.456, seq=7)
    payload = item.as_dict()
    assert set(payload) == {"account_id", "account_name", "stage", "message", "ts", "seq"}
    assert payload["seq"] == 7
    assert payload["ts"] == 123.456


def test_progress_item_rounds_timestamp() -> None:
    payload = ProgressItem(1, "a", "probe", "m", 1.23456, seq=1).as_dict()
    assert payload["ts"] == 1.235


# ---------- 缓冲与增量拉取 ----------


def test_progress_seq_is_monotonic(store, account: Account) -> None:
    mon = Monitor(store, interval=10)
    for i in range(5):
        mon.emit_progress(account, "probe", f"第 {i} 条")
    assert mon.progress_since()["seq"] == 5


def test_progress_since_returns_only_newer(store, account: Account) -> None:
    mon = Monitor(store, interval=10)
    for i in range(5):
        mon.emit_progress(account, "probe", f"第 {i} 条")

    got = mon.progress_since(seq=3)
    assert [i["message"] for i in got["items"]] == ["第 3 条", "第 4 条"]
    assert got["seq"] == 5


def test_progress_since_zero_returns_all(store, account: Account) -> None:
    mon = Monitor(store, interval=10)
    mon.emit_progress(account, "start", "a")
    assert len(mon.progress_since(seq=0)["items"]) == 1


def test_progress_since_future_seq_returns_empty(store, account: Account) -> None:
    mon = Monitor(store, interval=10)
    mon.emit_progress(account, "start", "a")
    assert mon.progress_since(seq=999)["items"] == []


def test_progress_filter_by_account(store) -> None:  # noqa: ANN001
    a1 = Account(id=1, name="甲", portal_url="http://a", username="u", password="p")
    a2 = Account(id=2, name="乙", portal_url="http://b", username="u", password="p")
    mon = Monitor(store, interval=10)
    mon.emit_progress(a1, "probe", "甲的进度")
    mon.emit_progress(a2, "probe", "乙的进度")
    mon.emit_progress(a1, "done", "甲结束了")

    only1 = mon.progress_since(seq=0, account_id=1)
    assert [i["message"] for i in only1["items"]] == ["甲的进度", "甲结束了"]
    assert only1["items"][0]["account_name"] == "甲"

    only2 = mon.progress_since(seq=0, account_id=2)
    assert [i["message"] for i in only2["items"]] == ["乙的进度"]


def test_progress_buffer_is_capped(store, account: Account) -> None:
    """环形缓冲不能无限涨，否则长跑服务会把内存吃满。"""
    mon = Monitor(store, interval=10, progress_cap=10)
    for i in range(50):
        mon.emit_progress(account, "probe", f"第 {i} 条")

    items = mon.progress_since(seq=0)["items"]
    assert len(items) == 10
    assert items[0]["message"] == "第 40 条"
    assert items[-1]["message"] == "第 49 条"
    assert mon.progress_since(seq=0)["seq"] == 50


def test_progress_cap_has_floor(store, account: Account) -> None:
    mon = Monitor(store, interval=10, progress_cap=0)
    assert mon.progress_cap == 10


def test_latest_progress_returns_tail(store, account: Account) -> None:
    mon = Monitor(store, interval=10)
    for i in range(20):
        mon.emit_progress(account, "probe", f"第 {i} 条")

    latest = mon.latest_progress(limit=5)
    assert len(latest["items"]) == 5
    assert latest["items"][-1]["message"] == "第 19 条"
    assert latest["seq"] == 20


def test_latest_progress_on_empty_buffer(store) -> None:  # noqa: ANN001
    payload = Monitor(store, interval=10).latest_progress()
    assert payload["items"] == []
    assert payload["seq"] == 0


def test_default_progress_cap_is_sane() -> None:
    assert 50 <= DEFAULT_PROGRESS_CAP <= 10_000


# ---------- 订阅者 ----------


def test_on_progress_callback_receives_each_item(store, account: Account) -> None:
    seen: list[dict] = []
    mon = Monitor(store, interval=10, on_progress=seen.append)
    mon.emit_progress(account, "probe", "a")
    mon.emit_progress(account, "done", "b")

    assert [i["message"] for i in seen] == ["a", "b"]
    assert all(i["account_id"] == account.id for i in seen)


def test_on_progress_exception_does_not_break_monitoring(
    store, account: Account, online_probe
) -> None:  # noqa: ANN001
    """订阅者抛异常不能让检测链路崩掉。"""
    store.add_account(account)

    def bad_callback(_item: dict) -> None:
        raise RuntimeError("前端挂了")

    mon = Monitor(store, interval=10, on_progress=bad_callback)
    results = mon.check_all()

    assert len(results) == 1 and results[0]["online"] is True


def test_no_callback_still_buffers(store, account: Account) -> None:
    mon = Monitor(store, interval=10)
    mon.emit_progress(account, "probe", "a")
    assert len(mon.progress_since()["items"]) == 1


# ---------- 在线路径的阶段顺序 ----------


def test_online_path_emits_start_probe_done(store, account: Account, online_probe) -> None:  # noqa: ANN001
    """已在线时应立刻反馈「无需登录」，不能让用户干等。"""
    acc_id = store.add_account(account)
    seen: list[dict] = []
    mon = Monitor(store, interval=10, on_progress=seen.append)

    mon.check_account(store.get_account(acc_id))

    stages = [i["stage"] for i in seen]
    assert stages[0] == "start"
    assert "probe" in stages
    assert stages[-1] == "done"
    assert "retry" not in stages  # 在线时不该出现登录重试


def test_online_path_done_message_mentions_no_login(store, account: Account, online_probe) -> None:  # noqa: ANN001
    acc_id = store.add_account(account)
    seen: list[dict] = []
    Monitor(store, interval=10, on_progress=seen.append).check_account(store.get_account(acc_id))
    assert any("无需登录" in i["message"] for i in seen)


# ---------- 掉线路径的阶段顺序 ----------


def test_offline_path_emits_full_stage_sequence(
    store, account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """掉线时必须逐阶段反馈：发现掉线 → 尝试 → 连接 → 解析 → 提交 → 复检 → 完成。"""
    from campusnet import monitor as monitor_mod
    from campusnet.portal import LoginResult

    state = {"online": False}

    def fake_probe(url: str, timeout: float = 5.0, insecure: bool = False):
        return monitor_mod.probe_mod.ProbeResult(state["online"], "online" if state["online"] else "redirected", "", url)

    monkeypatch.setattr(monitor_mod.probe_mod, "probe", fake_probe)

    def fake_login(account_obj, headless=True, on_progress=None):  # noqa: ANN001
        if on_progress:
            on_progress("connect", "正在连接认证门户")
            on_progress("parse", "正在识别登录表单")
            on_progress("submit", "以 POST 提交账号")
            on_progress("verify", "正在复检")
        state["online"] = True
        return LoginResult("success", "登录成功")

    monkeypatch.setattr(monitor_mod.portal, "login", fake_login)

    acc = account
    acc.max_retries = 2
    acc_id = store.add_account(acc)
    seen: list[dict] = []
    mon = Monitor(store, interval=10, on_progress=seen.append)
    mon.check_account(store.get_account(acc_id))

    stages = [i["stage"] for i in seen]
    for expected in ("start", "probe", "offline", "retry", "connect", "parse", "submit", "verify"):
        assert expected in stages, f"缺少阶段 {expected}：{stages}"
    assert stages.index("offline") < stages.index("retry")


def test_offline_path_reports_retry_count(store, account: Account, monkeypatch) -> None:  # noqa: ANN001
    """重试时必须报「第 N/M 次」，用户才知道还要等多久。"""
    from campusnet import monitor as monitor_mod
    from campusnet.portal import LoginResult

    def fake_probe(url: str, timeout: float = 5.0, insecure: bool = False):
        return monitor_mod.probe_mod.ProbeResult(False, "redirected", "", url)

    monkeypatch.setattr(monitor_mod.probe_mod, "probe", fake_probe)
    monkeypatch.setattr(
        monitor_mod.portal, "login", lambda *a, **k: LoginResult("failed", "密码错误")
    )
    monkeypatch.setattr("time.sleep", lambda *_a: None)

    acc = account
    acc.max_retries = 3
    acc_id = store.add_account(acc)
    seen: list[dict] = []
    Monitor(store, interval=10, on_progress=seen.append).check_account(store.get_account(acc_id))

    retry_msgs = [i["message"] for i in seen if i["stage"] == "retry"]
    assert len(retry_msgs) == 3
    assert "1/3" in retry_msgs[0]
    assert "3/3" in retry_msgs[2]


def test_failure_path_ends_with_fail(store, account: Account, monkeypatch) -> None:  # noqa: ANN001
    from campusnet import monitor as monitor_mod
    from campusnet.portal import LoginResult

    def fake_probe(url: str, timeout: float = 5.0, insecure: bool = False):
        return monitor_mod.probe_mod.ProbeResult(False, "redirected", "", url)

    monkeypatch.setattr(monitor_mod.probe_mod, "probe", fake_probe)
    monkeypatch.setattr(
        monitor_mod.portal, "login", lambda *a, **k: LoginResult("failed", "密码错误")
    )
    monkeypatch.setattr("time.sleep", lambda *_a: None)

    acc = account
    acc.max_retries = 1
    acc_id = store.add_account(acc)
    seen: list[dict] = []
    Monitor(store, interval=10, on_progress=seen.append).check_account(store.get_account(acc_id))

    assert seen[-1]["stage"] == "fail"
    assert "密码错误" in seen[-1]["message"]


def test_portal_success_but_probe_blocked_emits_warn(
    store, account: Account, monkeypatch
) -> None:  # noqa: ANN001
    """门户说成功但复检仍不通时，必须明确告知，不能报成功。"""
    from campusnet import monitor as monitor_mod
    from campusnet.portal import LoginResult

    def fake_probe(url: str, timeout: float = 5.0, insecure: bool = False):
        return monitor_mod.probe_mod.ProbeResult(False, "redirected", "", url)

    monkeypatch.setattr(monitor_mod.probe_mod, "probe", fake_probe)
    monkeypatch.setattr(
        monitor_mod.portal, "login", lambda *a, **k: LoginResult("success", "门户说成功")
    )
    monkeypatch.setattr("time.sleep", lambda *_a: None)

    acc = account
    acc.max_retries = 2
    acc_id = store.add_account(acc)
    seen: list[dict] = []
    Monitor(store, interval=10, on_progress=seen.append).check_account(store.get_account(acc_id))

    warns = [i["message"] for i in seen if i["stage"] == "warn"]
    assert any("复检仍未放行" in m for m in warns)


def test_stop_event_aborts_with_message(store, account: Account, monkeypatch) -> None:  # noqa: ANN001
    """监控被停止时应告知「中断」，而不是静默返回。"""
    from campusnet import monitor as monitor_mod
    from campusnet.portal import LoginResult

    def fake_probe(url: str, timeout: float = 5.0, insecure: bool = False):
        return monitor_mod.probe_mod.ProbeResult(False, "redirected", "", url)

    monkeypatch.setattr(monitor_mod.probe_mod, "probe", fake_probe)

    def fake_login(*_a, **_k):
        mon._stop.set()
        return LoginResult("failed", "门户不可达")

    monkeypatch.setattr(monitor_mod.portal, "login", fake_login)

    acc = account
    acc.max_retries = 5
    acc_id = store.add_account(acc)
    seen: list[dict] = []
    mon = Monitor(store, interval=10, on_progress=seen.append)
    mon.check_account(store.get_account(acc_id))

    assert any(i["stage"] == "abort" for i in seen)
    assert len([i for i in seen if i["stage"] == "retry"]) == 1  # 停止后不再重试