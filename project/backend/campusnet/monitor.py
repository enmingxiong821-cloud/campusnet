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
DEFAULT_PROGRESS_CAP = 300
PRUNE_LOG_INTERVAL = 300.0

# 裁剪统计存这张 settings 表的行键：单行覆盖，不增长、不参与 events 裁剪
PRUNE_STATS_KEY = "prune_stats"

# 门户会话查询的超时。比登录短：它只是给「探活失败」补一个判据，
# 卡太久会拖住整轮检测。
AUTH_STATE_TIMEOUT = 5.0


@dataclass
class ProgressItem:
    """一条实时进度：谁、在干什么、什么时候。"""

    account_id: int
    account_name: str
    stage: str
    message: str
    ts: float
    seq: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "account_name": self.account_name,
            "stage": self.stage,
            "message": self.message,
            "ts": round(self.ts, 3),
            "seq": self.seq,
        }


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
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        progress_cap: int = DEFAULT_PROGRESS_CAP,
        prune_log_interval: float = PRUNE_LOG_INTERVAL,
    ) -> None:
        self.store = store
        self.interval = self._clamp(interval)
        self.headless = headless
        self.on_event = on_event
        self.on_progress = on_progress
        self.progress_cap = max(10, int(progress_cap))
        self.prune_log_interval = max(0.0, float(prune_log_interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._progress: list[ProgressItem] = []
        self._progress_seq = 0
        self._progress_lock = threading.Lock()
        self._last_pruned_at: float | None = None
        self._prune_log_suppressed = 0
        self._prune_total_removed = 0
        self._prune_announced = False
        self.last_results: list[dict[str, Any]] = []
        self.running = False
        # 只有后台循环里的检测才该被 stop() 打断。
        # 手动「立即检测」走的是同一条路径，但监控线程通常没在跑，
        # 若不分开会话，用户点一次检测就被自己的 stop 打断成假失败。
        self.running_loop = False

    # ---------- 实时进度 ----------
    def emit_progress(self, account: Account, stage: str, message: str) -> None:
        """记录一条进度并推给订阅者。

        环形缓冲 + seq 单调递增：前端拿 seq 增量拉取，
        断线重连也不会漏消息或重复。
        """
        with self._progress_lock:
            self._progress_seq += 1
            item = ProgressItem(
                account_id=account.id,
                account_name=account.name,
                stage=stage,
                message=message,
                ts=time.time(),
                seq=self._progress_seq,
            )
            self._progress.append(item)
            if len(self._progress) > self.progress_cap:
                del self._progress[: len(self._progress) - self.progress_cap]
        if self.on_progress:
            try:
                self.on_progress(item.as_dict())
            except Exception:  # noqa: BLE001 - 订阅者异常不能影响检测
                pass

    def progress_since(self, seq: int = 0, account_id: int | None = None) -> dict[str, Any]:
        with self._progress_lock:
            items = [p for p in self._progress if p.seq > seq]
            if account_id is not None:
                items = [p for p in items if p.account_id == account_id]
            return {
                "items": [p.as_dict() for p in items],
                "seq": self._progress_seq,
            }

    def latest_progress(self, limit: int = 30) -> dict[str, Any]:
        with self._progress_lock:
            items = self._progress[-max(1, min(limit, self.progress_cap)) :]
            return {
                "items": [p.as_dict() for p in items],
                "seq": self._progress_seq,
            }

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
        self.running_loop = True
        try:
            while not self._stop.is_set():
                try:
                    self.check_all()
                except Exception as exc:  # noqa: BLE001 - 循环线程不能因单次异常退出
                    self.store.add_event(f"监控循环异常: {exc}", level="error", stage="monitor")
                self._stop.wait(self.interval)
        finally:
            self.running_loop = False

    def _portal_recheck(self, account: Account) -> tuple[bool | None, str]:
        """向门户复核本机会话是否存在。

        返回 (True, 说明) = 门户说在线；False = 门户说没会话；
        None = 问不出来，调用方按探活结论处理。

        非 SRUN 门户直接短路返回 None，不发任何请求 ——
        Dr.COM 这类门户没有 /cgi-bin/rad_user_info，白打一次必然 404。
        """
        base = (account.portal_url or "").strip()
        if not base:
            return None, "未配置门户地址"
        try:
            from . import srun as srun_mod
        except ImportError:  # pragma: no cover - 同包模块，正常不会缺
            return None, "srun 模块不可用"
        if not (account.login_mode == "srun" or srun_mod.is_srun_portal(base)):
            return None, "非 SRUN 门户"

        # stop() 只等 5 秒，这里最多再花 AUTH_STATE_TIMEOUT。
        # 已被要求停止时不要再发起新请求，否则 stop() 会超时返回 False，
        # 留下一个还在发包的僵尸线程。
        if self._stop.is_set() and self.running_loop:
            return None, "收到停止指令，跳过门户复核"

        config = portal.srun_config_for(account, timeout=AUTH_STATE_TIMEOUT)
        try:
            return srun_mod.auth_state(config)
        except Exception as exc:  # noqa: BLE001 - 判据查询失败绝不能打断检测
            return None, f"{type(exc).__name__}: {exc}"

    def check_account(self, account: Account) -> CheckOutcome:
        """探活一次；离线则按 max_retries 重试登录。

        每个阶段都 emit_progress，调用方能实时看到「卡在哪一步」。
        """
        started = time.perf_counter()
        self.emit_progress(account, "start", f"开始检测 {account.name}")
        self.emit_progress(account, "probe", f"正在探测 {account.probe_url}")
        check = probe_mod.probe(account.probe_url)
        if check.online:
            self.emit_progress(account, "done", "网络已放行，无需登录")
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

        # 探活失败先别急着认定掉线。canary 地址在校园网里常年被墙、被 CDN 拦，
        # 拿门户自己的会话表复核一次，能挡掉绝大多数假掉线 ——
        # 否则一轮 60 秒的监控会对着根本没掉线的账号反复重登。
        #
        # 只在探活失败时查：在线时门户查询纯属多余的一次网络往返。
        self.emit_progress(account, "verify", "探活未放行，正在向门户复核会话状态")
        auth_ok, auth_detail = self._portal_recheck(account)
        if auth_ok is True:
            self.emit_progress(account, "done", f"门户确认在线（{auth_detail}）")
            outcome = CheckOutcome(
                account.id, account.name, True, STATE_ONLINE, auth_detail, check.detail
            )
            self.store.set_state(account.id, STATE_ONLINE)
            self.store.add_event(
                f"{account.name}: 探活失败但门户确认在线（{check.status}）— {auth_detail}",
                level="info",
                stage="portal_state",
                state=STATE_ONLINE,
                account_id=account.id,
            )
            return outcome
        if auth_ok is False:
            self.emit_progress(account, "offline", f"门户确认无会话（{auth_detail}）")
        else:
            self.emit_progress(account, "warn", f"门户状态问不出来（{auth_detail}），按探活结果处理")

        self.emit_progress(
            account, "offline", f"检测到掉线（{check.status}），准备登录重连"
        )

        attempts = 0
        last = portal.LoginResult("failed", "未执行登录", "")
        aborted = False
        for attempt in range(1, max(1, account.max_retries) + 1):
            if self._stop.is_set():
                # 只在后台循环里才响应停止指令。
                # 手动点「立即检测」时监控本来就没开，
                # 若也在这里中断，用户会看到 attempts=0 的假失败。
                if self.running_loop:
                    self.emit_progress(account, "abort", "收到停止指令，中断重连")
                    aborted = True
                    break
            attempts = attempt
            total = max(1, account.max_retries)
            self.emit_progress(
                account, "retry", f"第 {attempt}/{total} 次尝试登录"
            )

            def _progress(stage: str, message: str, _acc: Account = account) -> None:
                self.emit_progress(_acc, stage, message)

            last = portal.login(account, headless=self.headless, on_progress=_progress)
            self.store.add_event(
                f"{account.name}: 第 {attempt} 次登录 -> {last.state} ({last.message})",
                level="info" if last.ok else "warning",
                stage="login",
                state=last.state,
                account_id=account.id,
            )
            if last.ok:
                self.emit_progress(account, "verify", "门户已受理，复检网络状态")
                confirm = probe_mod.probe(account.probe_url)
                if confirm.online:
                    self.emit_progress(account, "done", f"重连成功（{last.message}）")
                    break
                self.emit_progress(account, "warn", "门户说成功，但复检仍未放行")
            else:
                self.emit_progress(account, "warn", f"登录未成功：{last.message}")

            if attempt < max(1, account.max_retries):
                wait = min(3 * attempt, 8)
                self.emit_progress(account, "wait", f"{wait} 秒后重试")
                time.sleep(wait)

        elapsed = time.perf_counter() - started
        if last.ok:
            state = STATE_LOGGED_IN
            self.store.set_state(account.id, state)
            return CheckOutcome(
                account.id, account.name, True, state, last.message, last.detail, attempts, elapsed
            )

        if aborted:
            # 是我们自己叫停的，不是重连失败。
            # 落到下面那条 error 分支会有两个后果：事件日志出现一条
            # 「重连未成功 - 未执行登录」的假错误（根本没登录过），
            # 而状态被置成 failed，前端把 offline/failed 一起算进「需要关注」，
            # 于是用户主动停一次监控，账号就凭空多出一个红标。
            # 状态保持 offline：探活确实没放行，这是事实，不该掩盖。
            self.store.set_state(account.id, STATE_OFFLINE)
            self.store.add_event(
                f"{account.name}: 检测已中断（收到停止指令），未执行登录",
                level="warning",
                stage="login",
                state=STATE_OFFLINE,
                account_id=account.id,
            )
            return CheckOutcome(
                account.id,
                account.name,
                False,
                STATE_OFFLINE,
                "检测已中断：收到停止指令",
                last.detail,
                attempts,
                elapsed,
            )

        state = STATE_FAILED if last.state == "failed" else STATE_OFFLINE
        self.store.set_state(account.id, state)
        self.emit_progress(account, "fail", f"重连未成功：{last.message}")
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

        裁剪统计必须放在 settings 表，不能放events：
        公告一旦写进 events，下一轮裁剪就会把它删掉，
        于是"限频计数器认为已记录、实际记录已消失"，
        长跑下来 2000 条里塞满裁剪公告（实测 341 条）。
        settings 是单行覆盖，不增长也不参与裁剪。
        """
        try:
            keep = int(self.store.get_setting("max_events", DEFAULT_MAX_EVENTS) or DEFAULT_MAX_EVENTS)
            removed = self.store.prune_events(keep)
            if not removed:
                return

            now = time.time()
            self._last_pruned_at = now
            self._prune_log_suppressed += 1
            # 累计值要从 settings 续接：重启后新建的 Monitor 计数器是 0，
            # 直接用实例字段会把上一段进程的累计覆盖掉。
            previous = self.store.get_setting(PRUNE_STATS_KEY, None)
            carried = int(previous.get("total_removed", 0)) if isinstance(previous, dict) else 0
            self._prune_total_removed = carried + removed
            self.store.set_setting(
                PRUNE_STATS_KEY,
                {
                    "last_removed": removed,
                    "last_at": now,
                    "total_removed": self._prune_total_removed,
                    "keep": keep,
                    "suppressed": self._prune_log_suppressed - 1,
                },
            )

            # 只在裁剪首次生效时留一条事件，之后靠 settings 查统计
            if not self._prune_announced:
                self._prune_announced = True
                self.store.add_event(
                    f"事件日志已启用自动裁剪（保留最近 {keep} 条），"
                    f"本次裁掉 {removed} 条；后续统计见 settings.{PRUNE_STATS_KEY}",
                    level="info",
                    stage="maintenance",
                )
        except Exception as exc:  # noqa: BLE001
            self.store.add_event(f"事件裁剪失败: {exc}", level="error", stage="maintenance")

    def prune_stats(self) -> dict[str, Any]:
        """裁剪统计，从 settings 读，不受 events 裁剪影响。"""
        data = self.store.get_setting(PRUNE_STATS_KEY, None)
        if isinstance(data, dict):
            return data
        return {
            "last_removed": 0,
            "last_at": None,
            "total_removed": self._prune_total_removed,
            "keep": int(
                self.store.get_setting("max_events", DEFAULT_MAX_EVENTS) or DEFAULT_MAX_EVENTS
            ),
            "suppressed": self._prune_log_suppressed,
        }

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