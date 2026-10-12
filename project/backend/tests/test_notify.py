"""通知出口：阈值、恢复、配置刷新与失败容忍。"""

from __future__ import annotations

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
