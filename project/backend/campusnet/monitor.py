"""监控调度：周期探活 → 掉线触发重连 → 事件落库。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import portal, probe as probe_mod
from .storage import (
    STATE_FAILED,
    STATE_LOGGED_IN,
    STATE_OFFLINE,
    STATE_ONLINE,
    STATE_UNKNOWN,
    Account,
    Store,
)

DEFAULT_INTERVAL = 60
MIN_INTERVAL = 10
MAX_INTERVAL = 3600
DEFAULT_MAX_EVENTS = 2000


@dataclass
class CheckOutcome:
    account_id: int
    account_name: str
    online: bool
    state: str
    message: str
    probe_detail: str = ""
    attempts: int = 0
    elapsed: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "account_name": self.account_name,
            "online": self.online,
            "state": self.state,
            "message": self.message,
            "probe_detail": self.probe_detail,
            "attempts": self.attempts,
            "elapsed": round(self.elapsed, 3),
        }


class Monitor:
    """后台线程循环器；同一时刻只允许一个 check_all 在跑。"""

    def __init__(
        self,
        store: Store,
        interval: int = DEFAULT_INTERVAL,
        headless: bool = True,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.store = store
        self.interval = self._clamp(interval)
        self.headless = headless
        self.on_event = on_event
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last_results: list[dict[str, Any]] = []
        self.running = False

    @staticmethod
    def _clamp(value: int) -> int:
        return max(MIN_INTERVAL, min(MAX_INTERVAL, int(value)))

    # ---------- 生命周期 ----------
    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="campusnet-monitor", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            alive = self._thread.is_alive()
            self._thread = None
            return not alive
        return True

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---------- 检查逻辑 ----------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_all()
            except Exception as exc:  # noqa: BLE001 - 循环线程不能因单次异常退出
                self.store.add_event(f"监控循环异常: {exc}", level="error", stage="monitor")
            self._stop.wait(self.interval)

    def check_account(self, account: Account) -> CheckOutcome:
        """探活一次；离线则按 max_retries 重试登录。"""
        started = time.perf_counter()
        check = probe_mod.probe(account.probe_url)
        if check.online:
            outcome = CheckOutcome(
                account.id, account.name, True, STATE_ONLINE, "网络已放行", check.detail
            )
            self.store.set_state(account.id, STATE_ONLINE)
            self.store.add_event(
                f"{account.name}: 网络已放行（{check.status}）",
                level="info",
                stage="probe",
                state=STATE_ONLINE,
                account_id=account.id,
            )
            return outcome

        self.store.set_state(account.id, STATE_OFFLINE)
        self.store.add_event(
            f"{account.name}: 探活失败 {check.status} - {check.detail}",
            level="warning",
            stage="probe",
            state=STATE_OFFLINE,
            account_id=account.id,
        )

        attempts = 0
        last = portal.LoginResult("failed", "未执行登录", "")
        for attempt in range(1, max(1, account.max_retries) + 1):
            if self._stop.is_set():
                break
            attempts = attempt
            last = portal.login(account, headless=self.headless)
            self.store.add_event(
                f"{account.name}: 第 {attempt} 次登录 -> {last.state} ({last.message})",
                level="info" if last.ok else "warning",
                stage="login",
                state=last.state,
                account_id=account.id,
            )
            if last.ok:
                confirm = probe_mod.probe(account.probe_url)
                if confirm.online:
                    break
            time.sleep(min(3 * attempt, 8))

        elapsed = time.perf_counter() - started
        if last.ok:
            state = STATE_LOGGED_IN
            self.store.set_state(account.id, state)
            return CheckOutcome(
                account.id, account.name, True, state, last.message, last.detail, attempts, elapsed
            )

        state = STATE_FAILED if last.state == "failed" else STATE_OFFLINE
        self.store.set_state(account.id, state)
        self.store.add_event(
            f"{account.name}: 重连未成功 - {last.message}",
            level="error",
            stage="login",
            state=state,
            account_id=account.id,
        )
        return CheckOutcome(
            account.id, account.name, False, state, last.message, last.detail, attempts, elapsed
        )

    def check_all(self) -> list[dict[str, Any]]:
        if not self._lock.acquire(blocking=False):
            return self.last_results
        self.running = True
        try:
            accounts = self.store.list_accounts(enabled_only=True)
            results: list[dict[str, Any]] = []
            for account in accounts:
                outcome = self.check_account(account)
                results.append(outcome.as_dict())
                if self.on_event:
                    self.on_event(outcome.as_dict())
            self.last_results = results
            self._prune_events()
            return results
        finally:
            self.running = False
            self._lock.release()

    def _prune_events(self) -> None:
        """每轮检查后裁剪历史事件。

        默认 60 秒一轮、每轮至少一条事件，不裁剪的话库会一直涨。
        裁剪本身出错只记事件，不影响本轮检测结果。
        """
        try:
            keep = int(self.store.get_setting("max_events", DEFAULT_MAX_EVENTS) or DEFAULT_MAX_EVENTS)
            removed = self.store.prune_events(keep)
            if removed:
                self.store.add_event(
                    f"已裁剪 {removed} 条历史事件（保留最近 {keep} 条）",
                    level="info",
                    stage="maintenance",
                )
        except Exception as exc:  # noqa: BLE001
            self.store.add_event(f"事件裁剪失败: {exc}", level="error", stage="maintenance")

    def status(self) -> dict[str, Any]:
        return {
            "alive": self.is_alive(),
            "interval": self.interval,
            "headless": self.headless,
            "checking": self.running,
            "last_results": self.last_results,
        }


@dataclass
class MonitorManager:
    """管理单个 Monitor 实例，支持热更新间隔。"""

    monitor: Monitor | None = None
    store: Store | None = None
    _saved: dict[str, Any] = field(default_factory=dict)

    def ensure(self, store: Store) -> Monitor:
        if self.monitor is None:
            interval = int(store.get_setting("interval", DEFAULT_INTERVAL) or DEFAULT_INTERVAL)
            headless = bool(store.get_setting("headless", True))
            self.monitor = Monitor(store, interval=interval, headless=headless)
            self.store = store
        return self.monitor

    def start(self, store: Store) -> Monitor:
        monitor = self.ensure(store)
        monitor.start()
        return monitor

    def stop(self) -> bool:
        if self.monitor:
            return self.monitor.stop()
        return True

    def restart(self, store: Store, interval: int | None = None) -> Monitor:
        self.stop()
        monitor = self.ensure(store)
        if interval is not None:
            monitor.interval = Monitor._clamp(interval)
            store.set_setting("interval", monitor.interval)
        monitor.start()
        return monitor