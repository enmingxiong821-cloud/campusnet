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

    assert notifier.observe(1, "宿舍", False, "offline") == "failing"
    assert notifier.observe(1, "宿舍", True, "online") == "recovered"


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
