"""通知出口：阈值、恢复、配置刷新与失败容忍。"""

from __future__ import annotations

import json

import pytest

from campusnet import notify


@pytest.fixture()
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """拦下真实 HTTP，记录每次发送的地址与负载。"""
    calls: list[tuple[str, dict]] = []

    def fake_post(url: str, payload: dict) -> bool:
        calls.append((url, payload))
        return True

    monkeypatch.setattr(notify, "_post", fake_post)
    return calls


def test_disabled_notifier_sends_nothing(sent: list) -> None:
    notifier = notify.Notifier("")

    assert notifier.observe(1, "宿舍", online=False, state="offline") == ""
    assert sent == []


def test_failure_notice_fires_once_at_threshold(sent: list) -> None:
    notifier = notify.Notifier("http://hook.test/x", after_failures=3)

    assert notifier.observe(1, "宿舍", False, "offline") == ""
    assert notifier.observe(1, "宿舍", False, "offline") == ""
    assert notifier.observe(1, "宿舍", False, "offline") == "failing"
    # 一直在失败也只提醒一次，不能每轮轰炸
    assert notifier.observe(1, "宿舍", False, "offline") == ""

    assert len(sent) == 1
    url, payload = sent[0]
    assert url == "http://hook.test/x"
    assert payload["kind"] == "failing"
    assert payload["msgtype"] == "text"
    assert "连续 3 次" in payload["text"]["content"]
    assert payload["content"] == payload["text"]["content"]


def test_recovery_after_failure_notifies(sent: list) -> None:
    notifier = notify.Notifier("http://hook.test/x", after_failures=1)

    assert notifier.observe(1, "宿舍", False, "offline") == "failing"
    assert notifier.observe(1, "宿舍", True, "online", "网络已放行") == "recovered"

    assert [payload["kind"] for _, payload in sent] == ["failing", "recovered"]
    assert "已恢复在线" in sent[1][1]["text"]["content"]


def test_healthy_account_never_notifies(sent: list) -> None:
    notifier = notify.Notifier("http://hook.test/x", after_failures=1)

    assert notifier.observe(1, "宿舍", True, "online") == ""
    assert sent == []


def test_send_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """发送失败只影响返回值与计数，不能抛异常打断监控。"""
    monkeypatch.setattr(notify, "_post", lambda url, payload: False)
    notifier = notify.Notifier("http://hook.test/x", after_failures=1)

    # 返回值必须是「什么都没发出去」：monitor 拿它决定要不要写
    # 「已发送…通知」那条日志，返回 kind 就等于让日志报一件没发生的事。
    assert notifier.observe(1, "宿舍", False, "offline") == ""
    assert notifier.observe(1, "宿舍", True, "online") == ""


def test_failed_failure_notice_retries_until_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """webhook 恰好不可达那一轮，不能把这次掉线记成「已通知」。

    原来先 `_notified.add()` 再发送，且丢掉发送结果：那一轮之后每轮都在
    `_notified` 上短路，掉线通知永远补不上。要等真发出去了才落这笔账。
    """
    outcomes = [False, False, True]
    bodies: list[dict] = []

    def flaky(url: str, payload: dict) -> bool:
        bodies.append(payload)
        return outcomes.pop(0)

    monkeypatch.setattr(notify, "_post", flaky)
    notifier = notify.Notifier("http://hook.test/x", after_failures=1)

    assert notifier.observe(1, "宿舍", False, "offline") == ""  # 头一轮发失败
    assert notifier.observe(1, "宿舍", False, "offline") == ""  # 接着重试
    assert notifier.observe(1, "宿舍", False, "offline") == "failing"  # 这次通了
    # 补上之后「只提醒一次」才生效，不能继续每轮轰炸
    assert notifier.observe(1, "宿舍", False, "offline") == ""

    assert len(bodies) == 3


def test_post_swallows_unreachable_host() -> None:
    """真发一次到关闭的端口：必须返回 False，而不是抛出去。"""
    payload = notify.build_payload("测试", "test")

    assert notify._post("http://127.0.0.1:1/hook", payload) is False


def test_from_store_reads_settings_and_env(store, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    store.set_setting("notify_url", "http://from-db/x")
    store.set_setting("notify_after_failures", 5)

    assert notify.Notifier.from_store(store).url == "http://from-db/x"
    assert notify.Notifier.from_store(store).after_failures == 5

    monkeypatch.setenv("CAMPUSNET_NOTIFY_URL", "http://from-env/x")
    assert notify.Notifier.from_store(store).url == "http://from-env/x"


def test_from_store_tolerates_bad_threshold(store) -> None:  # noqa: ANN001
    store.set_setting("notify_after_failures", "abc")

    assert (
        notify.Notifier.from_store(store).after_failures
        == notify.DEFAULT_AFTER_FAILURES
    )


def test_refresh_reuses_object_until_config_changes(store) -> None:  # noqa: ANN001
    store.set_setting("notify_url", "http://hook/x")
    first = notify.refresh(None, store)
    assert notify.refresh(first, store) is first

    store.set_setting("notify_url", "http://other/x")
    third = notify.refresh(first, store)

    assert third is not first
    assert third.url == "http://other/x"


def test_channel_url_maps_key_to_endpoint() -> None:
    assert notify.channel_url("bark", "abc") == "https://api.day.app/abc"
    assert notify.channel_url("serverchan", "sk") == "https://sctapi.ftqq.com/sk.send"
    assert notify.channel_url("wecom", "wk") == "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wk"
    assert notify.channel_url("dingtalk", "dt") == "https://oapi.dingtalk.com/robot/send?access_token=dt"
    assert notify.channel_url("ntfy", "topic") == "https://ntfy.sh/topic"
    assert notify.channel_url("telegram", "bot123/456") == "https://api.telegram.org/botbot123/sendMessage"
    assert notify.channel_url("generic", "x") == ""


def test_bark_payload_shape() -> None:
    url, method, body = notify.build_channel_request("bark", "key", "正文", title="标题")

    assert url == "https://api.day.app/key"
    assert method == "POST"
    data = json.loads(body)
    assert data["body"] == "正文"
    assert data["title"] == "标题"


def test_wecom_and_dingtalk_payload_shape() -> None:
    for channel in ("wecom", "dingtalk"):
        url, method, body = notify.build_channel_request(channel, "key", "正文")

        assert method == "POST"
        data = json.loads(body)
        assert data["msgtype"] == "text"
        assert data["text"]["content"] == "正文"


def test_serverchan_uses_get_with_query() -> None:
    url, method, body = notify.build_channel_request("serverchan", "key", "正文", title="标题")

    assert method == "GET"
    assert body is None
    assert "title=" in url
    assert "desp=" in url


def test_telegram_payload_shape() -> None:
    url, method, body = notify.build_channel_request("telegram", "bot/456", "正文")

    assert method == "POST"
    assert url == "https://api.telegram.org/botbot/sendMessage"
    data = json.loads(body)
    assert data["chat_id"] == "456"
    assert data["text"] == "正文"


def test_ntfy_sends_plain_text() -> None:
    url, method, body = notify.build_channel_request("ntfy", "topic", "正文")

    assert method == "POST"
    assert url == "https://ntfy.sh/topic"
    assert body == "正文".encode("utf-8")


def test_channel_notifier_enabled_by_key() -> None:
    assert notify.Notifier("", channel="bark").enabled is False
    assert notify.Notifier("", channel="bark", key="k").enabled is True


def test_send_event_respects_event_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        notify, "_send", lambda url, method="POST", body=None: calls.append(url) or True
    )
    notifier = notify.Notifier("", channel="bark", key="k", events=["power"])

    assert notifier.send_event("power", "关机了") is True
    assert notifier.send_event("pair", "配对了") is False  # pair 未开启
    assert len(calls) == 1


# ---------- 「发出去了才清账」 ----------


def test_recovered_bookkeeping_cleared_only_after_successful_send(
    sent: list,
) -> None:
    """恢复方向：发送成功后才清零计数、摘掉已提醒标记。"""
    notifier = notify.Notifier("http://hook.test/x", after_failures=1)

    assert notifier.observe(1, "宿舍", False, "offline") == "failing"
    assert notifier._failures[1] == 1
    assert 1 in notifier._notified

    assert notifier.observe(1, "宿舍", True, "online", "网络已放行") == "recovered"
    assert notifier._failures[1] == 0
    assert 1 not in notifier._notified


def test_recovered_send_failure_keeps_bookkeeping_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恢复通知发失败时不能提前清账，否则「好了」这条永远补不上。

    回归防护：原来在发送前就把 _failures 清零、把 _notified 摘掉，发送一失败
    就再也没机会补——下一轮 online=True 时 was_failing 已是 False，恢复通知消失。
    """
    notifier = notify.Notifier("http://hook.test/x", after_failures=1)
    monkeypatch.setattr(notify, "_post", lambda url, payload: True)
    assert notifier.observe(1, "宿舍", False, "offline") == "failing"

    monkeypatch.setattr(notify, "_post", lambda url, payload: False)  # webhook 挂了
    assert notifier.observe(1, "宿舍", True, "online") == ""
    assert notifier._failures[1] == 1, "没发出去就不该清零"
    assert 1 in notifier._notified, "没发出去就不该摘标记"

    # webhook 恢复后下一轮能补发，之后才清账
    monkeypatch.setattr(notify, "_post", lambda url, payload: True)
    assert notifier.observe(1, "宿舍", True, "online") == "recovered"
    assert notifier._failures[1] == 0 and 1 not in notifier._notified


def test_recovered_event_disabled_clears_without_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """recovered 开关关掉时不发通知，但计数照样清零、标记摘掉。"""
    calls: list = []
    monkeypatch.setattr(
        notify, "_post", lambda url, payload: calls.append(payload) or True
    )
    notifier = notify.Notifier(
        "http://hook.test/x", after_failures=1, events=["failing"]
    )

    assert notifier.observe(1, "宿舍", False, "offline") == "failing"
    assert notifier._failures[1] == 1 and 1 in notifier._notified

    assert notifier.observe(1, "宿舍", True, "online") == ""
    assert notifier._failures[1] == 0
    assert 1 not in notifier._notified
    assert len(calls) == 1  # 只发过掉线那一条


def test_send_exception_is_swallowed_and_keeps_bookkeeping(
    store, account, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """发送过程抛异常时，异常不能穿出去，恢复方向的记账也不能被改。

    真实链路里 _post / _send 都把异常吞成 False；这里把 _post 换成必抛，
    验证消费方 Monitor._notify 兜住异常、且恢复方向的清零发生在发送之后——
    发送炸了自然就没清账，下一轮还会重试。
    """
    from campusnet import notify as notify_mod
    from campusnet.monitor import CheckOutcome, Monitor
    from campusnet.storage import STATE_OFFLINE, STATE_ONLINE

    store.set_setting("notify_url", "http://hook/x")
    store.set_setting("notify_after_failures", 1)
    acc_id = store.add_account(account)
    acc = store.get_account(acc_id)
    mon = Monitor(store, interval=10)

    monkeypatch.setattr(notify_mod, "_post", lambda url, payload: True)
    mon._notify(acc, CheckOutcome(acc_id, acc.name, False, STATE_OFFLINE, "掉线"))
    assert acc_id in mon.notifier._notified
    assert mon.notifier._failures[acc_id] == 1

    def boom(url: str, payload: dict) -> bool:
        raise RuntimeError("webhook 炸了")

    monkeypatch.setattr(notify_mod, "_post", boom)
    # 不该抛出去：Monitor._notify 必须兜住并留一条告警
    mon._notify(acc, CheckOutcome(acc_id, acc.name, True, STATE_ONLINE, "已恢复"))

    assert mon.notifier._failures[acc_id] == 1, "发送异常不能提前清零"
    assert acc_id in mon.notifier._notified, "发送异常不能提前摘标记"
    assert any(
        "通知发送异常" in e["message"] for e in store.list_events(limit=10)
    )
