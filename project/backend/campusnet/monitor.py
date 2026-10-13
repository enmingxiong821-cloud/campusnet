"""监控调度：周期探活 → 掉线触发重连 → 事件落库。"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import portal, probe as probe_mod, wifi as wifi_mod
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

# 开机热身窗口：守护进程启动后的这段时间内按 warmup_interval 密集重试。
#
# 为什么需要：开机那一刻无线驱动、WlanSvc、SSID 广播往往还没就绪，
# 第一轮必然失败。早先固定按 interval（默认 60 秒）等，失败一次就干等一整轮，
# 而错过的窗口往往只有十几秒。开机自连的用户体感就是「开机后要等很久才联网」。
DEFAULT_WARMUP_SECONDS = 180
DEFAULT_WARMUP_INTERVAL = 15
# 常规期某轮失败后的快速重试间隔，不等满整个 interval。
# 掉线是分钟级事件，按 60 秒一轮去等，用户感知到的「恢复速度」被拉长了。
DEFAULT_RETRY_INTERVAL = 60

# 凭据被门户拒绝后的冷却时长：密码错、账号被停用这类「重试不会改结果」的失败，
# 再试也只是在给门户送无效请求（有些学校 5 次就锁号）。冷却期内跳过登录、
# 只保留探活，改对密码后随下一轮自然恢复。
CREDENTIAL_BACKOFF_SECONDS = 600


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
        warmup_seconds: float = DEFAULT_WARMUP_SECONDS,
        warmup_interval: int = DEFAULT_WARMUP_INTERVAL,
        retry_interval: int = DEFAULT_RETRY_INTERVAL,
    ) -> None:
        self.store = store
        self.interval = self._clamp(interval)
        # 热身间隔不套 _clamp：它要的是「短」，而 _clamp 的下界是
        # MIN_INTERVAL=10，套上去会把 15 秒抬到 15（恰好相同）但也会
        # 把用户想要的 5 秒抬成 10，违背「密集重试」的意图。
        # 这里只兜一个下限 1 秒，防止 0 或负数把守护进程变成忙等。
        self.warmup_seconds = max(0.0, float(warmup_seconds))
        self.warmup_interval = max(1, int(warmup_interval))
        self.retry_interval = max(1, int(retry_interval))
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
        # 通知出口：未配置 webhook 时 observe() 立刻返回，等于零开销。
        from .notify import Notifier

        self.notifier = Notifier.from_store(store)
        # 凭据失败冷却：account_id -> 冷却截止时刻（monotonic 秒）。
        # 只在「认证被门户明确拒绝」时写入，网络类失败不进这里，照常退避重试。
        self._credential_cooldown: dict[int, float] = {}
        # 冷却字典要自己的锁：self._lock 是 check_all 的可重入保护，
        # 在 check_account 期间一直被持有，复用它会死锁。
        self._cooldown_lock = threading.Lock()

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

    def progress_since(
        self, seq: int = 0, account_id: int | None = None
    ) -> dict[str, Any]:
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
        self._thread = threading.Thread(
            target=self._loop, name="campusnet-monitor", daemon=True
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            alive = self._thread.is_alive()
            if not alive:
                self._thread = None
            return not alive
        return True

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---------- 检查逻辑 ----------
    def _loop(self) -> None:
        """两阶段调度循环。

        借鉴 demo133/campusnet 对「开机连不上」的两个失败模型：

        1. 连得太早 —— 开机瞬间无线驱动 / WlanSvc / SSID 广播还没就绪，
           第一轮必然失败。所以启动后先走热身窗口（默认 180 秒内每
           warmup_interval 秒一轮），一轮成功就立刻转常规节奏，
           不把用户按在「开机后等很久才联网」的状态里。
        2. 掉线了干等 —— 常规期某轮失败后不该等满整个 interval，
           掉线是分钟级事件，用户要的是尽快重连，所以下一轮提前到
           retry_interval。

        节奏只由 _next_delay 决定，_stop.wait 保证 stop() 仍能立刻
        打断——这是守护进程唯一的中断路径，不能被 sleep 占住。
        """
        self.running_loop = True
        started_at = time.monotonic()
        try:
            while not self._stop.is_set():
                ok = True
                try:
                    results = self.check_all()
                    # 任一账号还没放行就继续留在热身窗口里密集重试。
                    # 键名必须是 online —— CheckOutcome.as_dict() 出的就是它。
                    # 这里曾写成 r.get("ok")，而 as_dict 里根本没有 ok 这个键，
                    # 于是 all() 恒为 False，热身窗口永远退不出去：
                    # interval 配 300 秒也只能每 15 秒跑一轮，
                    # 「一轮成功即转常规」这个核心承诺整个落空。
                    # 已有门户会话但连通未确认时不重复认证，按常规间隔复核，
                    # 避免密集空转；这不代表 online=True，CLI 仍返回失败码。
                    ok = (
                        all(
                            r.get("online") or r.get("state") == STATE_UNKNOWN
                            for r in results
                        )
                        if results
                        else False
                    )
                except Exception as exc:  # noqa: BLE001 - 循环线程不能因单次异常退出
                    self.store.add_event(
                        f"监控循环异常: {exc}", level="error", stage="monitor"
                    )
                    ok = False
                self._stop.wait(self._next_delay(started_at, ok))
        finally:
            self.running_loop = False

    def _next_delay(self, started_at: float, all_ok: bool) -> float:
        """下一轮前的等待秒数。

        热身窗口内一律用 warmup_interval，不管上一轮成不成功——
        此刻网络还没稳，失败的唯一合理解释就是「太早了」，加密重试才对。
        窗口内一旦全部放行就退回 interval。
        """
        in_warmup = (time.monotonic() - started_at) < self.warmup_seconds
        if in_warmup and not all_ok:
            return float(self.warmup_interval)
        if not all_ok:
            return float(self.retry_interval)
        return float(self.interval)

    def _check_network_identity(self, account: Account) -> None:
        """SSID 身份检查，结果只记事件，不阻断流程。

        不阻断是刻意的：热点下认证必然失败，但失败原因该由门户报，
        这里提前抛异常只会把真因盖掉。检查的价值在于事后能解释
        「为什么程序一直没登录」——那正是这个失败模型最难查的地方。

        配置项放在 extra_fields 里而不是新增字段：Account 的 schema
        已经定型，加字段要动建表与迁移，而 wifi_ssid 天然属于
        「按学校各自不同的零散配置」。
        """
        patterns = account.extra_fields.get("wifi_ssid") or ""
        wanted = [p for p in re.split(r"[,\s]+", patterns) if p]
        if not wanted:
            return
        try:
            check = wifi_mod.ensure_campus_wifi(wanted)
        except Exception as exc:  # noqa: BLE001 - 身份检查失败绝不能打断检测
            self.store.add_event(
                f"{account.name}: 网络身份检查异常 - {type(exc).__name__}: {exc}",
                level="warning",
                stage="wifi",
                account_id=account.id,
            )
            return
        if check.ok:
            self.emit_progress(account, "wifi", check.detail)
            return
        self.emit_progress(account, "warn", check.detail)
        self.store.add_event(
            f"{account.name}: {check.detail}",
            level="warning",
            stage="wifi",
            account_id=account.id,
        )

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
        """探活一次并把结果喂给通知出口；真正的检测在 _check_account。"""
        outcome = self._check_account(account)
        self._notify(account, outcome)
        return outcome

    def _notify(self, account: Account, outcome: CheckOutcome) -> None:
        """按结果发通知；通知出任何问题都不能影响检测本身。"""
        from .notify import refresh

        self.notifier = refresh(self.notifier, self.store)
        try:
            kind = self.notifier.observe(
                account.id, account.name, outcome.online, outcome.state, outcome.message
            )
        except Exception as exc:  # noqa: BLE001 - 通知失败绝不打断检测
            self.store.add_event(
                f"{account.name}: 通知发送异常 - {type(exc).__name__}: {exc}",
                level="warning",
                stage="notify",
                account_id=account.id,
            )
            return
        if kind:
            label = "恢复" if kind == "recovered" else "连续失败"
            self.store.add_event(
                f"{account.name}: 已发送{label}通知",
                level="info",
                stage="notify",
                state=outcome.state,
                account_id=account.id,
            )

    def _check_account(self, account: Account) -> CheckOutcome:
        """探活一次；离线则按 max_retries 重试登录。

        每个阶段都 emit_progress，调用方能实时看到「卡在哪一步」。
        """
        started = time.perf_counter()
        self.emit_progress(account, "start", f"开始检测 {account.name}")

        # 先问「连的是不是校园网」，再问「有没有网」。
        # 顺序反了会在热点上误判：手机热点同样放行 canary，
        # probe 会一路绿灯，程序从此认定已联网、再也不碰认证接口。
        # 配了 wifi_ssid 才查；没配就是零成本的有线/多网卡零影响。
        self._check_network_identity(account)

        self.emit_progress(account, "probe", f"正在探测 {account.probe_url}")
        check = probe_mod.probe_configured(account.probe_url)
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
            self.emit_progress(
                account, "warn", f"门户会话存在，但网络探活失败（{auth_detail}）"
            )
            outcome = CheckOutcome(
                account.id,
                account.name,
                False,
                STATE_UNKNOWN,
                f"门户会话存在，但网络连通未确认：{auth_detail}",
                check.detail,
            )
            self.store.set_state(account.id, STATE_UNKNOWN)
            self.store.add_event(
                f"{account.name}: 门户会话存在，跳过重复登录；探活失败（{check.status}）— {auth_detail}",
                level="warning",
                stage="portal_state",
                state=STATE_UNKNOWN,
                account_id=account.id,
            )
            return outcome
        if auth_ok is False:
            self.emit_progress(account, "offline", f"门户确认无会话（{auth_detail}）")
        else:
            self.emit_progress(
                account, "warn", f"门户状态问不出来（{auth_detail}），按探活结果处理"
            )

        self.emit_progress(
            account, "offline", f"检测到掉线（{check.status}），准备登录重连"
        )

        # 凭据被门户拒绝后进入冷却：再试也是同样的结果，只会把无效请求灌给门户
        # （有些学校 5 次就锁号）。冷却期内跳过登录、只保留探活，改对密码后随
        # 下一轮自然恢复。冷却只拦「登录」，不拦探活——网络真的通了照样报在线。
        with self._cooldown_lock:
            cooldown_until = self._credential_cooldown.get(account.id)
        now = time.monotonic()
        if cooldown_until and now < cooldown_until:
            remaining = int(cooldown_until - now)
            outcome = CheckOutcome(
                account.id,
                account.name,
                False,
                STATE_OFFLINE,
                f"认证被门户拒绝，冷却中（{remaining} 秒后自动重试；改对密码请更新账号）",
                check.detail,
                0,
                time.perf_counter() - started,
            )
            self.store.set_state(account.id, STATE_OFFLINE)
            self.emit_progress(account, "wait", outcome.message)
            self.store.add_event(
                f"{account.name}: 凭据失败冷却中，跳过登录（{remaining} 秒后重试）",
                level="warning",
                stage="login",
                state=STATE_OFFLINE,
                account_id=account.id,
            )
            return outcome

        attempts = 0
        network_confirmed = False
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
            self.emit_progress(account, "retry", f"第 {attempt}/{total} 次尝试登录")

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
                confirm = probe_mod.probe_configured(account.probe_url)
                if confirm.online:
                    network_confirmed = True
                    self.emit_progress(account, "done", f"重连成功（{last.message}）")
                    break
                self.emit_progress(account, "warn", "门户说成功，但复检仍未放行")
            else:
                self.emit_progress(account, "warn", f"登录未成功：{last.message}")
                if last.terminal:
                    # 凭据被门户明确拒绝：这一轮剩下的重试都是白送请求，立刻收手。
                    # 冷却写进 _credential_cooldown，下一轮起跳过登录直到冷却结束。
                    self.emit_progress(
                        account, "wait", "凭据被门户拒绝，本轮停止重试并进入冷却"
                    )
                    break

            if attempt < max(1, account.max_retries):
                wait = min(3 * attempt, 8)
                self.emit_progress(account, "wait", f"{wait} 秒后重试")
                time.sleep(wait)

        if last.terminal:
            with self._cooldown_lock:
                self._credential_cooldown[account.id] = (
                    time.monotonic() + CREDENTIAL_BACKOFF_SECONDS
                )
            self.store.add_event(
                f"{account.name}: 凭据被门户拒绝（{last.message}），"
                f"进入 {CREDENTIAL_BACKOFF_SECONDS} 秒冷却，不再反复提交登录",
                level="warning",
                stage="login",
                state=STATE_OFFLINE,
                account_id=account.id,
            )

        elapsed = time.perf_counter() - started
        if last.ok:
            # 登录成功即清掉冷却：可能是用户改了密码，也可能是上次的拒绝已过期。
            with self._cooldown_lock:
                self._credential_cooldown.pop(account.id, None)
            state = STATE_LOGGED_IN if network_confirmed else STATE_OFFLINE
            self.store.set_state(account.id, state)
            return CheckOutcome(
                account.id,
                account.name,
                network_confirmed,
                state,
                last.message if network_confirmed else "门户已受理，但网络复检仍未放行",
                last.detail,
                attempts,
                elapsed,
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
            account.id,
            account.name,
            False,
            state,
            last.message,
            last.detail,
            attempts,
            elapsed,
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
            keep = int(
                self.store.get_setting("max_events", DEFAULT_MAX_EVENTS)
                or DEFAULT_MAX_EVENTS
            )
            removed = self.store.prune_events(keep)
            if not removed:
                return

            now = time.time()
            self._last_pruned_at = now
            self._prune_log_suppressed += 1
            # 累计值要从 settings 续接：重启后新建的 Monitor 计数器是 0，
            # 直接用实例字段会把上一段进程的累计覆盖掉。
            previous = self.store.get_setting(PRUNE_STATS_KEY, None)
            carried = (
                int(previous.get("total_removed", 0))
                if isinstance(previous, dict)
                else 0
            )
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
            self.store.add_event(
                f"事件裁剪失败: {exc}", level="error", stage="maintenance"
            )

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
                self.store.get_setting("max_events", DEFAULT_MAX_EVENTS)
                or DEFAULT_MAX_EVENTS
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
            interval = int(
                store.get_setting("interval", DEFAULT_INTERVAL) or DEFAULT_INTERVAL
            )
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
        if not self.stop():
            raise RuntimeError("监控线程未能在超时时间内停止，拒绝启动第二个监控线程")
        monitor = self.ensure(store)
        if interval is not None:
            monitor.interval = Monitor._clamp(interval)
            store.set_setting("interval", monitor.interval)
        monitor.start()
        return monitor
