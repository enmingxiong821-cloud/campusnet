"""电源体检：回答「断电 / 关机之后，人不在现场时这台机器还能不能被救起来」。

与 doctor 的分工：doctor 回答「无人值守认证现在处不处于正常状态」，
power prep 回答「远程开关机这件事的前置条件齐不齐」——快速启动关没关、
网卡允不允许魔术包唤醒、远程桌面开没开、有没有自动登录、时间准不准、
BIOS 来电自启设没设。能自动修的（快速启动、网卡唤醒、睡眠超时）用 `--fix` 一键改，
改不动的（BIOS、自动登录口令、需要管理员）标成「需人工」并说明怎么改、怎么改回去。

只做检测与本地系统设置，不发网络请求（时间比对除外，联网失败就跳过）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from . import tls as tls_mod

OK = "ok"
FIXED = "fixed"
WARN = "warn"
MANUAL = "manual"

CREATE_NO_WINDOW = 0x08000000
TIME_SKEW_THRESHOLD_SECONDS = 300  # 5 分钟：超过就可能导致 HMAC/HTTPS 握手失败
DEFAULT_TIME_TARGET = "https://www.baidu.com"


@dataclass
class PowerItem:
    name: str
    status: str
    detail: str
    fixable: bool = False
    undo: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run(args: list[str], timeout: int = 20) -> tuple[bool, str]:
    """跑一条系统命令，返回 (是否成功, 合并后的文本输出)。"""
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout, **kwargs)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False, ""
    text = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
    return proc.returncode == 0, text


def _is_admin() -> bool:
    """当前进程是否提权（管理员）。无法判断时按「非管理员」处理。"""
    ok, _ = _run(["net", "session"])
    return ok


def _reg_query_value(key: str, value: str) -> str | None:
    """读注册表一个值；读不到返回 None。"""
    ok, text = _run(["reg", "query", key, "/v", value])
    if not ok:
        return None
    match = re.search(rf"{re.escape(value)}\s+REG_\w+\s+(.+)", text)
    return match.group(1).strip() if match else None


def _set_reg_dword(key: str, value: str, data: int) -> bool:
    ok, _ = _run(
        ["reg", "add", key, "/v", value, "/t", "REG_DWORD", "/d", str(data), "/f"]
    )
    return ok


# ---------------------------------------------------------------- Windows


def _check_admin() -> PowerItem:
    if _is_admin():
        return PowerItem("管理员权限", OK, "当前已是管理员，可自动修复各项系统设置")
    return PowerItem(
        "管理员权限",
        MANUAL,
        "当前不是管理员：快速启动、网卡唤醒、远程桌面这些项需要一次提权才能改"
        "（右键桌面上的「校园网自连」快捷方式选「以管理员身份运行」，"
        "或在管理员终端里执行 python utils/run.py power prep --fix）",
        fixable=False,
    )


def _check_fast_startup(fix: bool) -> PowerItem:
    """快速启动（HiberbootEnabled=1）会让「关机」实为休眠，WOL 与来电行为都不符合预期。"""
    key = r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power"
    undo = "改回去：reg add \"HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power\" "
    undo += "/v HiberbootEnabled /t REG_DWORD /d 1 /f"
    raw = _reg_query_value(key, "HiberbootEnabled")
    enabled = raw in ("0x1", "1")
    if enabled:
        if fix:
            if not _is_admin():
                return PowerItem(
                    "快速启动", MANUAL, "需要管理员权限才能关闭，本次未能修改",
                    fixable=True, undo=undo,
                )
            if _set_reg_dword(key, "HiberbootEnabled", 0):
                return PowerItem(
                    "快速启动", FIXED, "已关闭（HiberbootEnabled=0）",
                    fixable=True, undo=undo,
                )
            return PowerItem(
                "快速启动", MANUAL, "修改失败，本次未能关闭", fixable=True, undo=undo
            )
        return PowerItem(
            "快速启动",
            WARN,
            "已开启——「关机」其实是休眠，会导致 WOL 与断电重来电后的行为不符合预期；"
            "用 --fix 可关闭（需管理员）",
            fixable=True,
            undo=undo,
        )
    return PowerItem("快速启动", OK, "已关闭，关机就是真关机", fixable=True, undo=undo)


def _wake_magic_adapters() -> tuple[list[str], list[str]]:
    """返回 (允许魔术包唤醒的网卡名, 不允许的网卡名)。"""
    script = (
        "Get-NetAdapterPowerManagement -ErrorAction SilentlyContinue | "
        "Select-Object Name,WakeOnMagicPacket,WakeOnPattern | ConvertTo-Json -Compress"
    )
    ok, text = _run(["powershell", "-NoProfile", "-Command", script])
    if not ok or not text.strip():
        return [], []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [], []
    if isinstance(data, dict):
        data = [data]
    allowed, denied = [], []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name", "")).strip()
        if not name:
            continue
        magic = str(item.get("WakeOnMagicPacket", "")).lower() == "enabled"
        pattern = str(item.get("WakeOnPattern", "")).lower() == "enabled"
        (allowed if magic or pattern else denied).append(name)
    return allowed, denied


def _check_wake_on_lan(fix: bool) -> PowerItem:
    undo = "改回去：Set-NetAdapterPowerManagement -Name <网卡名> "
    undo += "-WakeOnMagicPacket Disabled -WakeOnPattern Disabled"
    allowed, denied = _wake_magic_adapters()
    if not allowed and not denied:
        # PowerShell 或模块不可用：无法判定，报「需人工」而不是假装 OK。
        # fixable 必须是 False：读网卡状态靠 Get-NetAdapterPowerManagement，改它靠同
        # 一模块的 Set-*，模块不可用时 --fix 会落回这条分支，什么也改不了。标成可修会
        # 让 CLI（cli/power.py 的「将尝试自动修复」）和控制台（index.html 的确认框）都
        # 把它算进待修清单，用户确认一圈却什么都没发生；这条 detail 自己也写着
        # 「请手动…」，前后矛盾。
        return PowerItem(
            "网卡魔术包唤醒",
            MANUAL,
            "无法读取网卡电源管理（PowerShell 的 NetAdapter 模块不可用或命令失败）；"
            "请手动在设备管理器 → 网卡 → 电源管理里勾选「允许此设备唤醒计算机」",
            fixable=False,
            undo=undo,
        )
    if denied:
        if fix:
            if not _is_admin():
                return PowerItem(
                    "网卡魔术包唤醒", MANUAL,
                    "需要管理员权限才能打开，本次未能修改", fixable=True, undo=undo,
                )
            fixed, failed = [], []
            for name in denied:
                # 网卡名是用户可控的（可以改成带单引号的），直接拼进 -Command 会被
                # 当成命令分隔；PowerShell 里单引号内写两个单引号才是转义。
                safe_name = name.replace("'", "''")
                script = (
                    f"Set-NetAdapterPowerManagement -Name '{safe_name}'"
                    f" -WakeOnMagicPacket Enabled -WakeOnPattern Enabled"
                )
                if _run(["powershell", "-NoProfile", "-Command", script])[0]:
                    fixed.append(name)
                else:
                    failed.append(name)
            if not failed:
                return PowerItem(
                    "网卡魔术包唤醒", FIXED, f"已为 {', '.join(fixed)} 打开魔术包唤醒",
                    fixable=True, undo=undo,
                )
            return PowerItem(
                "网卡魔术包唤醒", MANUAL,
                f"{', '.join(failed)} 修改失败，其余已打开",
                fixable=True, undo=undo,
            )
        return PowerItem(
            "网卡魔术包唤醒",
            WARN,
            f"{', '.join(denied)} 未开启魔术包唤醒，关机后无法被 WOL 叫醒；"
            "用 --fix 可打开（需管理员）",
            fixable=True,
            undo=undo,
        )
    return PowerItem(
        "网卡魔术包唤醒", OK, f"{', '.join(allowed)} 允许魔术包唤醒", fixable=True, undo=undo
    )


def _check_wake_armed() -> PowerItem:
    """powercfg 里当前真正能唤醒系统的设备。"""
    ok, text = _run(["powercfg", "/devicequery", "wake_armed"])
    if not ok:
        return PowerItem("可唤醒设备", MANUAL, "无法读取（powercfg 不可用）")
    devices = [line.strip() for line in text.splitlines() if line.strip()]
    if not devices:
        return PowerItem("可唤醒设备", WARN, "当前没有任何设备被允许唤醒——WOL 会失效")
    shown = ", ".join(devices[:3]) + ("…" if len(devices) > 3 else "")
    return PowerItem("可唤醒设备", OK, shown)


def _check_sleep_timeout(fix: bool) -> PowerItem:
    """睡眠/休眠超时：远程开机场景下应只关显示器、不睡眠（睡眠后需要人按电源键）。"""
    undo = "改回去：powercfg /change standby-timeout-ac <原分钟数>"
    ok, text = _run(["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"])
    if not ok:
        return PowerItem("睡眠超时", MANUAL, "无法读取（powercfg /query 失败）")
    match = re.search(
        r"(?:当前交流电源设置索引|Current AC Power Setting Index)\s*:\s*"
        r"(0x[0-9a-fA-F]+|\d+)",
        text,
    )
    if not match:
        return PowerItem("睡眠超时", MANUAL, "无法解析睡眠超时设置")
    raw = match.group(1)
    minutes = int(raw, 16) if raw.startswith("0x") else int(raw)
    if minutes != 0:
        if fix:
            if not _is_admin():
                return PowerItem(
                    "睡眠超时", MANUAL, "需要管理员权限才能修改，本次未能修改",
                    fixable=True, undo=undo,
                )
            if _run(["powercfg", "/change", "standby-timeout-ac", "0"])[0]:
                return PowerItem(
                    "睡眠超时", FIXED, "已设为 0（永不休眠，只关显示器）",
                    fixable=True, undo=undo,
                )
            return PowerItem(
                "睡眠超时", MANUAL, "修改失败，本次未能修改", fixable=True, undo=undo
            )
        return PowerItem(
            "睡眠超时",
            WARN,
            f"交流电源下 {minutes} 分钟后睡眠——远程场景下建议设为 0；用 --fix 可改（需管理员）",
            fixable=True,
            undo=undo,
        )
    return PowerItem("睡眠超时", OK, "已设为 0（永不休眠）", fixable=True, undo=undo)


def _check_remote_desktop(fix: bool) -> PowerItem:
    """远程桌面（fDenyTSConnections=0 才开启）。"""
    key = r"HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server"
    undo = "改回去：reg add \"HKLM\\SYSTEM\\CurrentControlSet\\Control\\Terminal Server\" "
    undo += "/v fDenyTSConnections /t REG_DWORD /d 1 /f"
    raw = _reg_query_value(key, "fDenyTSConnections")
    disabled = raw not in ("0x0", "0")
    if disabled:
        if fix:
            if not _is_admin():
                return PowerItem(
                    "远程桌面", MANUAL, "需要管理员权限才能开启，本次未能修改",
                    fixable=True, undo=undo,
                )
            if _set_reg_dword(key, "fDenyTSConnections", 0):
                return PowerItem(
                    "远程桌面", FIXED, "已开启（fDenyTSConnections=0）",
                    fixable=True, undo=undo,
                )
            return PowerItem(
                "远程桌面", MANUAL, "修改失败，本次未能开启", fixable=True, undo=undo
            )
        return PowerItem(
            "远程桌面",
            WARN,
            "未开启——人在外面就看不到桌面；用 --fix 可开启（需管理员）",
            fixable=True,
            undo=undo,
        )
    return PowerItem("远程桌面", OK, "已开启", fixable=True, undo=undo)


def _check_auto_logon() -> PowerItem:
    """自动登录：只提示，不自动写（涉及登录口令，且 DPAPI 绑当前用户）。"""
    key = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
    raw = _reg_query_value(key, "AutoAdminLogon")
    if raw == "1":
        return PowerItem("自动登录", OK, "已开启，断电重来电后能直接进桌面再自动认证")
    return PowerItem(
        "自动登录",
        MANUAL,
        "未开启——断电重来电后机器会停在登录界面，服务（登录后语义）起不来；"
        "可用 Sysinternals Autologon 或系统 netplwiz 配置（涉及口令，本工具不代填）",
    )


def _check_time_skew() -> PowerItem:
    """时间偏差：断电后主板电池老化会导致时钟漂移，HMAC/HTTPS 握手会失败。"""
    target = os.environ.get("CAMPUSNET_TIME_SERVER", DEFAULT_TIME_TARGET)
    # 关校验是必须的：时钟本身就是被测对象，证书有效期先一步失败的话，
    # 这一项永远测不出偏差，反而变成常绿。
    context = tls_mod.portal_context(insecure=True)
    request = urllib.request.Request(target, headers={"User-Agent": "campusnet"})
    try:
        with urllib.request.urlopen(request, timeout=5, context=context) as response:
            header = response.headers.get("Date", "")
    except Exception:  # noqa: BLE001 - 连不上就跳过，不把体检判成红
        return PowerItem("时间偏差", OK, "未能联网比对（已跳过）")
    if not header:
        return PowerItem("时间偏差", OK, "目标未返回 Date 头（已跳过）")
    try:
        server_time = datetime.strptime(header, "%a, %d %b %Y %H:%M:%S %Z").replace(
            tzinfo=timezone.utc
        )
        skew = abs((datetime.now(timezone.utc) - server_time).total_seconds())
    except ValueError:
        return PowerItem("时间偏差", OK, "无法解析目标时间（已跳过）")
    if skew > TIME_SKEW_THRESHOLD_SECONDS:
        return PowerItem(
            "时间偏差",
            WARN,
            f"与目标相差约 {int(skew // 60)} 分钟，超过 5 分钟——断电后主板电池可能老化，"
            "建议开启系统时间自动同步或更换主板电池",
        )
    return PowerItem("时间偏差", OK, f"与目标相差约 {int(skew)} 秒")


def _check_bios_ac_recovery() -> PowerItem:
    """BIOS「来电自启」读不到（软件层面拿不到），只给主板型号与键位指引。"""
    ok, text = _run(["wmic", "baseboard", "get", "Manufacturer,Product"])
    board = "未知主板"
    if ok:
        match = re.search(r"(.+?)\s+(.+)", text.splitlines()[-1] if text.splitlines() else "")
        if match:
            board = match.group(1).strip() or "未知主板"
    return PowerItem(
        "BIOS 来电自启",
        MANUAL,
        f"{board}：软件读不到这项设置，需进 BIOS 手动改——开机按 Del/F2 进 BIOS，"
        "找 Restore AC Power Loss / After Power Loss / AC Power Recovery，设为 Power On",
    )


def _windows_checks(fix: bool) -> list[PowerItem]:
    return [
        _check_admin(),
        _check_fast_startup(fix),
        _check_wake_on_lan(fix),
        _check_wake_armed(),
        _check_sleep_timeout(fix),
        _check_remote_desktop(fix),
        _check_auto_logon(),
        _check_time_skew(),
        _check_bios_ac_recovery(),
    ]


# ---------------------------------------------------------------- Linux / macOS


def _check_linux_wol(fix: bool) -> PowerItem:
    undo = "改回去：sudo ethtool -s <网卡> wol d"
    exe = shutil.which("ethtool")
    if not exe:
        return PowerItem("网卡 WOL", MANUAL, "未安装 ethtool（sudo apt install ethtool）")
    ok, text = _run(["ip", "-o", "link"])
    if not ok:
        return PowerItem("网卡 WOL", MANUAL, "无法列出网卡")
    ifaces = re.findall(r":\s*([\w.-]+):", text)
    enabled, disabled = [], []
    for iface in ifaces:
        if iface == "lo":
            continue
        code, out = _run([exe, iface])
        if not code:
            continue
        match = re.search(r"Wake-on:\s*(\S+)", out)
        supports_g = match and "g" in match.group(1)
        (enabled if supports_g else disabled).append(iface)
    if disabled:
        if fix:
            # 逐块试：多网卡时只修第一块却报「已为 a, b 打开」是假消息，
            # 用户会以为 b 也开了，回头唤醒失败根本想不到是这里。
            fixed = [
                iface
                for iface in disabled
                if _run(["sudo", "-n", exe, "-s", iface, "wol", "g"])[0]
            ]
            if fixed:
                rest = [iface for iface in disabled if iface not in fixed]
                detail = f"已为 {', '.join(fixed)} 打开魔术包唤醒"
                if rest:
                    detail += f"；{', '.join(rest)} 未能修改（免密 sudo 不可用）"
                return PowerItem("网卡 WOL", FIXED, detail, fixable=True, undo=undo)
            return PowerItem(
                "网卡 WOL", MANUAL,
                "需要 sudo 权限才能打开，本次未能修改（免密 sudo 不可用）",
                fixable=True, undo=undo,
            )
        return PowerItem(
            "网卡 WOL",
            WARN,
            f"{', '.join(disabled)} 未开启 WOL；需 sudo ethtool -s <网卡> wol g（--fix 尝试用免密 sudo）",
            fixable=True,
            undo=undo,
        )
    if enabled:
        return PowerItem("网卡 WOL", OK, f"{', '.join(enabled)} 支持魔术包唤醒", fixable=True, undo=undo)
    return PowerItem("网卡 WOL", MANUAL, "没有可判断的物理网卡")


def _check_linux_rtcwake() -> PowerItem:
    if shutil.which("rtcwake"):
        return PowerItem("定时唤醒", OK, "rtcwake 可用，可用它做「定时唤醒」")
    return PowerItem("定时唤醒", MANUAL, "未安装 rtcwake（util-linux 包内）")


def _linux_checks(fix: bool) -> list[PowerItem]:
    return [_check_linux_wol(fix), _check_linux_rtcwake(), _check_time_skew()]


def _check_macos_womp(fix: bool) -> PowerItem:
    undo = "改回去：sudo pmset -a womp 0"
    ok, text = _run(["pmset", "-g"])
    if not ok:
        return PowerItem("网卡唤醒", MANUAL, "无法读取 pmset 设置")
    match = re.search(r"^\s*womp\s+(\d+)", text, re.MULTILINE)
    on = match and match.group(1) == "1"
    if not on:
        if fix and _run(["sudo", "-n", "pmset", "-a", "womp", "1"])[0]:
            return PowerItem(
                "网卡唤醒", FIXED, "已打开 wake on magic packet（womp=1）",
                fixable=True, undo=undo,
            )
        if fix:
            return PowerItem(
                "网卡唤醒", MANUAL, "需要 sudo 权限才能打开，本次未能修改（免密 sudo 不可用）",
                fixable=True, undo=undo,
            )
        return PowerItem(
            "网卡唤醒", WARN, "womp=0，关机后无法被 WOL 唤醒；用 --fix 可改（需 sudo）",
            fixable=True, undo=undo,
        )
    return PowerItem("网卡唤醒", OK, "womp=1，支持魔术包唤醒", fixable=True, undo=undo)


def _macos_checks(fix: bool) -> list[PowerItem]:
    return [_check_macos_womp(fix), _check_time_skew()]


# ---------------------------------------------------------------- 编排


def run(
    fix: bool = False, yes: bool = False, plat: str | None = None
) -> list[PowerItem]:
    """执行电源体检；fix=True 时对可自动修复项尝试修复。

    yes 仅用于 CLI 的非交互确认（在命令行层处理），这里保留参数便于签名统一。
    """
    del yes  # 交互确认由 CLI/API 层处理
    name = plat or sys.platform
    if name == "win32":
        checks = _windows_checks(fix)
    elif name == "darwin":
        checks = _macos_checks(fix)
    else:
        checks = _linux_checks(fix)
    return checks


def render(items: list[PowerItem], *, as_json: bool = False) -> str:
    if as_json:
        return json.dumps([item.as_dict() for item in items], ensure_ascii=False, indent=2)
    label = {OK: "OK  ", FIXED: "已修", WARN: "WARN", MANUAL: "人工"}
    lines = [
        f"[{label.get(item.status, item.status)}] {item.name}: {item.detail}"
        + (f"\n      如何改回去：{item.undo}" if item.undo else "")
        for item in items
    ]
    return "\n".join(lines)


def exit_code(items: list[PowerItem]) -> int:
    """MANUAL 也算「有未竟事项」返回 1；OK/FIXED/WARN 都是能用的状态。"""
    return 1 if any(item.status == MANUAL for item in items) else 0
