"""首次使用的一键配置：把「下载完 → 能跑起来」之间要手动做的事串成一条命令。

设计取舍：运行本体只用标准库，所以大多数人不需要装任何依赖。默认只做
「检查 + 生成快捷方式 + 自检 + 告诉你下一步」，把可选依赖（Playwright）
留成显式开关——否则一上来就下载几百 MB 的浏览器，是最劝退的做法。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from dataclasses import dataclass, field

from . import bootstart as bootstart_mod
from . import doctor as doctor_mod
from . import shortcuts as shortcuts_mod

MIN_PYTHON = (3, 10)
_MARKS = {"ok": "✓", "warn": "!", "fail": "✗"}


@dataclass
class SetupReport:
    lines: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers

    def render(self) -> str:
        body = "\n".join(self.lines)
        if self.blockers:
            body += "\n\n需要先解决的问题：\n" + "\n".join(
                f"  ✗ {item}" for item in self.blockers
            )
        return body


def check_python() -> str | None:
    """返回阻塞原因；None 表示版本可用。"""
    if sys.version_info >= MIN_PYTHON:
        return None
    current = ".".join(str(item) for item in sys.version_info[:3])
    need = ".".join(str(item) for item in MIN_PYTHON)
    return f"需要 Python {need} 及以上，当前是 {current}"


def browser_ready() -> bool:
    return importlib.util.find_spec("playwright") is not None


def install_browser_deps() -> tuple[bool, str]:
    """安装 Playwright 与 Chromium（只在用户显式要求时装）。"""
    try:
        pip = subprocess.run(
            [sys.executable, "-m", "pip", "install", "playwright"],
            capture_output=True,
            timeout=600,
        )
        if pip.returncode != 0:
            tail = (pip.stderr or b"")[-200:].decode("utf-8", "replace")
            return False, f"pip 安装 playwright 失败：{tail.strip()}"
        chromium = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            timeout=900,
        )
        if chromium.returncode != 0:
            tail = (chromium.stderr or b"")[-200:].decode("utf-8", "replace")
            return False, f"下载 chromium 失败：{tail.strip()}"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, "已安装 Playwright 与 Chromium（浏览器登录模式可用）"


def run_setup(
    *,
    with_browser: bool = False,
    make_shortcuts: bool = True,
    platform: str | None = None,
    check: bool = True,
) -> SetupReport:
    """跑完首次配置；platform / check 参数供测试注入。"""
    report = SetupReport()
    version = ".".join(str(item) for item in sys.version_info[:3])
    report.lines.append(f"1/4 Python 环境：{version}")
    blocker = check_python()
    if blocker:
        report.blockers.append(blocker)
        report.lines.append(f"    ✗ {blocker}")
        return report
    report.lines.append("    ✓ 版本满足要求（运行本体只用标准库，无需装依赖）")

    report.lines.append("2/4 桌面快捷方式")
    if make_shortcuts:
        ok, message = shortcuts_mod.create(None, platform=platform)
        report.lines.append(f"    {'✓' if ok else '!'} {message}")
        if not ok:
            report.lines.append("    （不影响使用，可稍后手动跑 run.py shortcut）")
    else:
        report.lines.append("    - 已跳过（--no-shortcut）")

    report.lines.append("3/4 浏览器登录模式（可选，仅验证码 / CAS 门户需要）")
    if browser_ready():
        report.lines.append("    ✓ Playwright 已安装")
    elif with_browser:
        ok, message = install_browser_deps()
        report.lines.append(f"    {'✓' if ok else '!'} {message}")
        if not ok:
            report.blockers.append(message)
    else:
        report.lines.append(
            "    - 未安装；需要时跑 run.py setup --with-browser"
        )

    report.lines.append("4/4 自检")
    if check:
        checks = doctor_mod.run(probe=False)
        failed = [item for item in checks if item.status == doctor_mod.FAIL]
        warned = [item for item in checks if item.status == doctor_mod.WARN]
        for item in checks:
            mark = _MARKS.get(item.status, "?")
            report.lines.append(f"    {mark} {item.name}: {item.detail}")
        report.lines.append(
            f"    —— 通过 {len(checks) - len(failed) - len(warned)} 项，"
            f"提示 {len(warned)} 项，失败 {len(failed)} 项"
        )
        report.blockers.extend(
            f"{item.name}: {item.detail}" for item in failed
        )
    else:
        report.lines.append("    - 已跳过")

    report.lines.append("")
    report.lines.append(
        "下一步（都在桌面「校园网自连」这个入口的菜单里；没生成就改跑"
        " python utils/run.py menu）："
    )
    # 这一步在 Windows 与另外两个平台上做的是不同的事：Windows 走计划任务（要 UAC、
    # 要账户密码，换来「通电即认证」），Linux / macOS 只能注册用户级载体。原来这段
    # 两种平台共用一句话，等于对 Linux / macOS 用户承诺了一件做不到的事。
    if bootstart_mod.platform_name() == "windows":
        report.lines.append("  1. 选「注册开机自启」，同意 UAC 并输入密码")
        report.lines.append("     —— 做完这一步，通电就会自动认证，不需要任何人登录")
    else:
        report.lines.append("  1. 选「注册开机自启」，注册登录后自动连接")
        report.lines.append(f"     —— {bootstart_mod.unattended_hint()}")
    report.lines.append("  2. 选「添加账号」（门户地址 + 账号密码）")
    report.lines.append("  3. 手机访问：选「查看多端访问」，拿到地址与令牌")
    return report
