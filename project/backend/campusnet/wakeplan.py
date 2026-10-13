"""唤醒计划的调度核心：解析「每天/每周几点」的时间表，判断到点没有。

与设备清单一起构成「远程开机」的一半：WOL 是二层广播、跨不了路由，计划只对
与本机同一局域网的设备有效。计划写进 SQLite，由 monitor 线程每轮检测后顺带
调度一次（`should_fire` 保证每天每档最多发一次），不另起进程。

时间表语法（系统本地时间）：
- "07:30"          每天 07:30
- "mon-fri 07:30"  工作日 07:30
- "mon,wed 07:30"  周一和周三 07:30
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


@dataclass(frozen=True)
class Schedule:
    days: frozenset[int] | None  # None = 每天；否则 0=周一 .. 6=周日
    hour: int
    minute: int

    def matches(self, dt: datetime) -> bool:
        return self.days is None or dt.weekday() in self.days


def _parse_time(text: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def _parse_days(text: str) -> set[int] | None:
    out: set[int] = set()
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start, _, end = chunk.partition("-")
            left, right = _WEEKDAYS.get(start), _WEEKDAYS.get(end)
            if left is None or right is None:
                return None
            if left <= right:
                out.update(range(left, right + 1))
            else:  # 跨周，如 fri-mon
                out.update(range(left, 7))
                out.update(range(0, right + 1))
        else:
            day = _WEEKDAYS.get(chunk)
            if day is None:
                return None
            out.add(day)
    return out or None


def parse_schedule(spec: str) -> Schedule | None:
    """解析时间表；非法输入返回 None，调用方当作「这条计划配置错了」跳过。"""
    parts = (spec or "").strip().lower().split()
    if not parts:
        return None
    if len(parts) == 1:
        days = None
        time_part = parts[0]
    elif len(parts) == 2:
        days = _parse_days(parts[0])
        time_part = parts[1]
    else:
        return None
    tm = _parse_time(time_part)
    if tm is None:
        return None
    if len(parts) == 2 and days is None:
        return None
    return Schedule(None if days is None else frozenset(days), tm[0], tm[1])


def should_fire(
    schedule: Schedule, now: datetime, last_run: datetime | None
) -> bool:
    """判断「现在这一档」到点没有、以及这一档今天发过没有。

    last_run 是上次发送时间（或 None 表示从没发过）。规则：今天的触发时刻已过、
    且 last_run 早于今天这一档（或从没发过），才发——同一天不会重复发；
    机器停机跳过几天后，下一个到点日只补发一次，不会追着补发。
    """
    if not schedule.matches(now):
        return False
    today_fire = now.replace(
        hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0
    )
    if now < today_fire:
        return False
    if last_run is None:
        return True
    return last_run < today_fire


def parse_last_run(text: str | None) -> datetime | None:
    """把库里存的 "YYYY-MM-DD HH:MM:SS" 转回本地时间。"""
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def send_wol(mac: str, broadcast: str, port: int) -> str:
    """发一个 WOL 魔术包（复用 power 的发送逻辑）。"""
    from . import power

    return power.wake(mac, broadcast, port)
