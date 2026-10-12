"""网络身份：确认连的是校园网，而不是手机热点。

借鉴 demo133/campusnet 的失败模型 A。它那句结论值得抄在门口：
「不能问有没有网络，而要问连上的网络是不是校园网。」

开机自连场景下这个模型很致命：手机热点也有「自动连接」，开机瞬间
程序就切到热点并连上，此时探测 canary 一路绿灯，程序认为「已联网，
无需认证」，于是永远不会去碰认证接口。用户在热点上却以为自己已经
在校园网内，什么都想干不了。

判断依据是 SSID 文本匹配，所以它只能是配置驱动的软提示：
留空即关闭，一个 netsh 都不会发——有线用户与多网卡环境零影响。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any

CREATE_NO_WINDOW = 0x08000000
NETSH_TIMEOUT = 8.0
CONNECT_WAIT = 30.0


@dataclass
class WifiCheck:
    """一次身份检查的结果。ok 为 False 时 detail 说明原因。"""

    ok: bool
    detail: str
    ssid: str = ""


def _run_netsh(args: list[str], timeout: float = NETSH_TIMEOUT) -> tuple[bool, str]:
    """跑 netsh 并返回 (成功, 合并输出)。

    刻意不接受环境里的代理配置：这是本机系统调用，与 http_proxy 无关。
    """
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        # 只在 Windows 传。CPython 在 POSIX 分支遇到非零 creationflags
        # 会直接 raise ValueError，而那个 ValueError 不属于
        # (OSError, SubprocessError)，会漏出这个 except。
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout, **kwargs)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    return proc.returncode == 0, (out + err).strip()


def current_ssid() -> str:
    """当前连上的无线网 SSID；有线连接或网卡关闭时返回空串。

    netsh 输出是本地化的，中英文的字段名不同，所以按 `SSID :` 的形状
    取值而不写死关键词。顺带跳过 BSSID 行——它也叫 SSID 段的邻居，
    误读会拿到一串 MAC 地址。
    """
    ok, out = _run_netsh(["netsh", "wlan", "show", "interfaces"])
    if not ok:
        return ""
    for line in out.splitlines():
        if "BSSID" in line.upper():
            continue
        m = re.match(r"\s*SSID\s*:\s*(.+?)\s*$", line, re.IGNORECASE)
        if m:
            # 必须 strip：无线网卡开着但 SSID 段为空时，正则会回溯匹配到
            # 那一个空格并返回 ' '。它是 truthy，会让调用方跳过「有线」分支，
            # 于是空 SSID 反而被判成「连着别的网」。
            return m.group(1).strip()
    return ""


def ensure_campus_wifi(ssid_patterns: list[str], connect_wait: float = CONNECT_WAIT) -> WifiCheck:
    """确保连上校园网；ssid_patterns 为空时直接放行，不发任何命令。

    pattern 列表是「或」关系，全部不匹配才算走到别的网。
    大小写不敏感——校园网 SSID 常写作 CAMPUS / campus 两种。

    连不上不抛错也不阻断登录：调用方拿到 ok=False 后应记一条 warning
    再照常走认证流程。热点下认证必然失败，但那是门户该报的错，
    不该由这里提前抛一个含糊的异常。
    """
    wanted = [p.strip() for p in ssid_patterns if p and p.strip()]
    if not wanted:
        # 空白项等价于未配置。判断放到 strip 之后而不是只判列表非空：
        # 用户从配置文件读出 ["", ""] 时不该被算成「配了规则且不匹配」。
        return WifiCheck(True, "未配置 SSID 规则，跳过网络身份检查")

    ssid = current_ssid()
    if not ssid:
        # 有线连接是常态，不能因为读不到 SSID 就判定「不在校园网」
        return WifiCheck(True, "未连接无线网（有线或网卡关闭），跳过身份检查")

    if any(p.lower() in ssid.lower() for p in wanted):
        return WifiCheck(True, f"已连接校园网（{ssid}）", ssid)

    return WifiCheck(
        False,
        f"当前连接的是「{ssid}」，不属于配置的校园网（{', '.join(wanted)}）；"
        f"热点下探测会一直通过，程序会误以为已联网、永远不会去认证",
        ssid,
    )


def connect_to(ssid: str) -> tuple[bool, str]:
    """切到指定 SSID。

    目前没有任何调用方，是留给手动排障的逃生口（脚本里直接 import 用）。
    刻意不接进监控主路径：开机瞬间自动 `netsh connect` 会加剧网卡抖动，
    且企业网常见隐藏 SSID 无法按名连接，副作用大于收益。
    身份检查保持「只告警不修复」。
    """
    return _run_netsh(["netsh", "wlan", "connect", f"name={ssid}"])
