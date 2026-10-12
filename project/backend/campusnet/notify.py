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


class Notifier:
    """按账号跟踪连续失败次数，只在恢复与达阈值时各发一条。"""

    def __init__(
        self, url: str = "", after_failures: int = DEFAULT_AFTER_FAILURES
    ) -> None:
        self.url = (url or "").strip()
        self.after_failures = max(1, int(after_failures))
        self._failures: dict[int, int] = {}
        self._notified: set[int] = set()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @classmethod
    def from_store(cls, store: Any) -> "Notifier":
        url = os.environ.get("CAMPUSNET_NOTIFY_URL", "").strip()
        if not url:
            url = str(store.get_setting("notify_url", "") or "").strip()
        raw = store.get_setting("notify_after_failures", DEFAULT_AFTER_FAILURES)
        try:
            after = int(raw)
        except (TypeError, ValueError):
            after = DEFAULT_AFTER_FAILURES
        return cls(url, after)

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
                interrupted = (
                    account_id in self._notified
                    or self._failures.get(account_id, 0) > 0
                )
                self._failures[account_id] = 0
                self._notified.discard(account_id)
                if not interrupted:
                    return ""
                kind = "recovered"
                text = f"✅ {account_name} 已恢复在线（{message or state}）"
            else:
                count = self._failures.get(account_id, 0) + 1
                self._failures[account_id] = count
                if count < self.after_failures or account_id in self._notified:
                    return ""
                self._notified.add(account_id)
                kind = "failing"
                text = f"⚠️ {account_name} 连续 {count} 次未连上（{message or state}）"
        self.send(kind, text, account=account_name, state=state)
        return kind

    def send(
        self, kind: str, text: str, account: str = "", state: str = ""
    ) -> bool:
        if not self.enabled:
            return False
        return _post(self.url, build_payload(text, kind, account, state))


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
    same = (current.url, current.after_failures) == (
        notifier.url,
        notifier.after_failures,
    )
    return notifier if same else current
