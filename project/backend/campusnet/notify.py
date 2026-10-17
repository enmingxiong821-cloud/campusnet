"""失败通知出口：把「恢复在线」与「连续失败」推给一个 webhook。

为什么是通用 webhook：钉钉、企业微信、Slack、自建服务都能用 webhook 转发，
不必为每家写适配器。配置读 settings（`notify_url` / `notify_after_failures`），
环境变量 `CAMPUSNET_NOTIFY_URL` 可临时覆盖，便于自启动脚本里单独启用。

两条硬约束：

1. 通知失败绝不影响监控：发送异常一律吞掉并返回 False；
2. 只在「状态变化」时发：恢复在线发一条，连续失败达阈值发一条，
   之后不再重复轰炸（同一轮失败持续多久都只提醒一次）。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from typing import Any

DEFAULT_AFTER_FAILURES = 3
TIMEOUT = 5.0


def build_payload(
    text: str, kind: str, account: str = "", state: str = ""
) -> dict[str, Any]:
    """一份能同时喂给钉钉、企业微信与自建服务的负载。

    钉钉与企业微信都要求 `{"msgtype": "text", "text": {"content": ...}}`；
    自建服务通常直接读顶层字段。两边都带上，多余的键会被对方忽略。
    """
    return {
        "msgtype": "text",
        "text": {"content": text},
        "content": text,
        "kind": kind,
        "account": account,
        "state": state,
        "ts": int(time.time()),
    }


def _post(url: str, payload: dict[str, Any]) -> bool:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return 200 <= int(response.status) < 300
    except Exception:  # noqa: BLE001 - 通知失败不能影响监控
        return False


def _send(url: str, method: str = "POST", body: bytes | None = None) -> bool:
    """按方法发请求；body 为 None 时走 GET（如 Server酱）。"""
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return 200 <= int(response.status) < 300
    except Exception:  # noqa: BLE001
        return False


# 渠道预设：选渠道 + 填 key，就不必手拼每个平台的 webhook URL。
CHANNELS: dict[str, str] = {
    "generic": "通用 Webhook",
    "bark": "Bark",
    "serverchan": "Server酱",
    "wecom": "企业微信机器人",
    "dingtalk": "钉钉机器人",
    "telegram": "Telegram Bot",
    "ntfy": "ntfy",
}


def channel_url(channel: str, key: str) -> str:
    """按渠道把 key 拼成最终 URL；generic 返回空串（走 notify_url）。"""
    key = (key or "").strip()
    if channel == "bark":
        return f"https://api.day.app/{key}"
    if channel == "serverchan":
        return f"https://sctapi.ftqq.com/{key}.send"
    if channel == "wecom":
        return f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
    if channel == "dingtalk":
        return f"https://oapi.dingtalk.com/robot/send?access_token={key}"
    if channel == "ntfy":
        return f"https://ntfy.sh/{key}"
    if channel == "telegram":
        token = key.partition("/")[0]  # key 形如 "<bot_token>/<chat_id>"
        return f"https://api.telegram.org/bot{token}/sendMessage"
    return ""


def build_channel_request(
    channel: str, key: str, text: str, title: str = ""
) -> tuple[str, str, bytes | None]:
    """构造一次通知请求，返回 (URL, HTTP方法, 请求体或 None)，不真正发送。

    每个渠道的负载形状在这里定义，测试据此断言 payload 形状，不必真发网络请求。
    """
    url = channel_url(channel, key)
    if channel == "serverchan":
        from urllib.parse import urlencode

        query = urlencode({"title": title or "校园网自连", "desp": text})
        return f"{url}?{query}", "GET", None
    if channel == "bark":
        body = json.dumps(
            {"title": title or "校园网自连", "body": text}, ensure_ascii=False
        ).encode("utf-8")
        return url, "POST", body
    if channel in ("wecom", "dingtalk"):
        body = json.dumps(
            {"msgtype": "text", "text": {"content": text}}, ensure_ascii=False
        ).encode("utf-8")
        return url, "POST", body
    if channel == "telegram":
        chat_id = key.partition("/")[2]
        body = json.dumps(
            {"chat_id": chat_id, "text": text}, ensure_ascii=False
        ).encode("utf-8")
        return url, "POST", body
    if channel == "ntfy":
        return url, "POST", text.encode("utf-8")
    return "", "POST", None  # generic 走 notify_url


class Notifier:
    """按账号跟踪连续失败次数，只在恢复与达阈值时各发一条；支持多渠道与事件开关。"""

    def __init__(
        self,
        url: str = "",
        after_failures: int = DEFAULT_AFTER_FAILURES,
        channel: str = "generic",
        key: str = "",
        events: list[str] | None = None,
    ) -> None:
        self.url = (url or "").strip()
        self.after_failures = max(1, int(after_failures))
        self.channel = channel if channel in CHANNELS else "generic"
        self.key = (key or "").strip()
        # 默认只发「掉线 / 恢复在线」，与旧行为一致；其余分类要显式打开。
        # 空列表是「一个都不发」的显式选择，不能落回默认——原来 events=[]
        # 会被当成没配置，用户在设置里关掉全部通知也照样收到两条。
        self.events = (
            {"recovered", "failing"} if events is None else set(events)
        )
        self._failures: dict[int, int] = {}
        self._notified: set[int] = set()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        if self.channel == "generic":
            return bool(self.url)
        return bool(self.key)

    @classmethod
    def from_store(cls, store: Any) -> "Notifier":
        channel = str(store.get_setting("notify_channel", "generic") or "").strip().lower()
        if channel not in CHANNELS:
            channel = "generic"
        url = os.environ.get("CAMPUSNET_NOTIFY_URL", "").strip()
        if not url:
            url = str(store.get_setting("notify_url", "") or "").strip()
        key = str(store.get_setting("notify_key", "") or "").strip()
        raw = store.get_setting("notify_after_failures", DEFAULT_AFTER_FAILURES)
        try:
            after = int(raw)
        except (TypeError, ValueError):
            after = DEFAULT_AFTER_FAILURES
        raw_events = store.get_setting("notify_events", None)
        events = [str(e) for e in raw_events] if isinstance(raw_events, list) else None
        return cls(url, after, channel, key, events)

    def event_enabled(self, category: str) -> bool:
        return category in self.events

    @staticmethod
    def _title(kind: str) -> str:
        return {
            "failing": "掉线",
            "recovered": "恢复在线",
            "power": "电源动作",
            "pair": "配对",
            "auth": "认证",
            "test": "测试",
        }.get(kind, kind)

    def observe(
        self,
        account_id: int,
        account_name: str,
        online: bool,
        state: str,
        message: str = "",
    ) -> str:
        """每轮检测后调用；返回实际发出的通知类型，没发则返回空串。"""
        if not self.enabled:
            return ""
        with self._lock:
            if online:
                # 只有真发过「掉线」才配发「恢复在线」。原来把 `_failures > 0`
                # 也算作曾失败，于是 after_failures=3 时「第 1 轮失败、第 2 轮
                # 恢复」会发出一条用户从没收到过前情的「已恢复在线」；链路每
                # 两轮抖动一次就无限重复发，正是本模块要避免的那种轰炸。
                was_failing = account_id in self._notified
                if not was_failing:
                    self._failures[account_id] = 0
                    return ""
                if not self.event_enabled("recovered"):
                    self._failures[account_id] = 0
                    self._notified.discard(account_id)
                    return ""
                kind = "recovered"
                text = f"✅ {account_name} 已恢复在线（{message or state}）"
            else:
                count = self._failures.get(account_id, 0) + 1
                self._failures[account_id] = count
                if count < self.after_failures or account_id in self._notified:
                    return ""
                if not self.event_enabled("failing"):
                    return ""
                kind = "failing"
                text = f"⚠️ {account_name} 连续 {count} 次未连上（{message or state}）"
        if not self.send(kind, text, account=account_name, state=state):
            # 没发出去就当没发过：返回空串，让调用方别把它记成「已发送」，
            # 也不要在 `_notified` 上落账——落了账下一轮就在这里短路，
            # webhook 恢复后这条「掉线」永远补不上（原来就是先记账再发送）。
            return ""
        with self._lock:
            if kind == "failing":
                self._notified.add(account_id)
            else:
                # 恢复通知也是「发出去了才清账」。原来在发送前就把失败计数清零、
                # 把已提醒标记摘掉，发送一失败就再也没机会补：下一轮 online=True
                # 时 was_failing 已经是 False，这条「已恢复在线」就此消失，
                # 人不在机器旁边只收到「掉线」、收不到「好了」。
                self._failures[account_id] = 0
                self._notified.discard(account_id)
        return kind

    def send(
        self, kind: str, text: str, account: str = "", state: str = ""
    ) -> bool:
        if not self.enabled:
            return False
        if self.channel == "generic":
            return _post(self.url, build_payload(text, kind, account, state))
        url, method, body = build_channel_request(
            self.channel, self.key, text, title=self._title(kind)
        )
        return _send(url, method, body)

    def send_event(self, category: str, text: str, state: str = "") -> bool:
        """按事件分类发一条通知，受 notify_events 开关控制。

        用于「电源动作」「配对」这类独立事件——不像 observe 那样要跟踪
        连续失败状态，只需判断该分类是否开启。
        """
        if not self.event_enabled(category):
            return False
        return self.send(category, text, state=state)


def send_test(notifier: Notifier) -> bool:
    """发一条明确标注为测试的消息，用于确认链路能通。"""
    return notifier.send("test", "校园网自连：测试通知 ✅", account="测试")


def refresh(notifier: "Notifier | None", store: Any) -> "Notifier":
    """按当前配置返回可用的 Notifier。

    配置没变就复用原对象，否则用户在控制台改完 webhook 得重启服务才生效，
    而「连续失败计数」也会被无谓清零。
    """
    try:
        current = Notifier.from_store(store)
    except Exception:  # noqa: BLE001 - 读配置失败不该影响检测
        return notifier or Notifier()
    if notifier is None:
        return current

    def _cfg(instance: Any) -> tuple:
        return (
            getattr(instance, "url", ""),
            getattr(instance, "after_failures", DEFAULT_AFTER_FAILURES),
            getattr(instance, "channel", "generic"),
            getattr(instance, "key", ""),
            getattr(instance, "events", {"recovered", "failing"}),
        )

    return notifier if _cfg(current) == _cfg(notifier) else current
