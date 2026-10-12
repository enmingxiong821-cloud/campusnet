"""交互式主菜单：桌面上只留一个快捷方式，其余操作都在这里选。

为什么这么做：把「启动服务 / 打开控制台 / 立即检测 / 自检 / 看日志 / 关机 / 重启 /
注册开机自启」拆成八个快捷方式，桌面上很乱，用户还得记住哪个是哪个。
合并成一个入口后，需要的只是按一次数字键。
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

from .bootstart import python_executable
from .paths import ROOT


@dataclass(frozen=True)
class MenuItem:
    label: str
    hint: str
    args: tuple[str, ...] = ()


ITEMS: tuple[MenuItem, ...] = (
    MenuItem("打开控制台（浏览器）", "看状态、加账号、改设置", ("console",)),
    MenuItem("启动服务", "前台运行，Ctrl+C 停止", ("serve",)),
    MenuItem("立即检测网络", "掉线会自动重连", ("check",)),
    MenuItem("查看自检报告", "端口/数据库/令牌/自启动/探活", ("doctor",)),
    MenuItem("查看多端访问", "手机该访问哪个地址、令牌是多少", ("access",)),
    MenuItem("查看运行日志", "最近 40 行", ("logs", "--lines", "40")),
    MenuItem("远程关机", "关机后需靠 WOL 或插座再开机", ("power", "off")),
    MenuItem("远程重启", "", ("power", "reboot")),
    MenuItem("注册开机自启", "开机即认证、无需登录（需要管理员权限）", ("bootstart", "install", "--mode", "task")),
    MenuItem("卸载开机自启", "", ("bootstart", "uninstall")),
)

REGISTER_INDEX = 9  # ITEMS 里「注册开机自启」是第 9 项（从 1 开始计数）


def _relaunch_elevated(args: list[str]) -> int:
    """用 UAC 提权重跑自己：注册「无需登录」的任务必须管理员权限。"""
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return 1
    inner = subprocess.list2cmdline(
        [str(python_executable()), str(ROOT / "utils" / "run.py"), *args]
    )
    quoted_inner = "'" + inner.replace("'", "''") + "'"
    script = (
        "Start-Process -Verb RunAs -FilePath 'cmd.exe' -ArgumentList "
        + quoted_inner
        + " -Wait"
    )
    try:
        subprocess.run([exe, "-NoProfile", "-Command", script], timeout=600)
    except (OSError, subprocess.SubprocessError):
        return 1
    return 0


def register_bootstart(ask: Callable[[str], str], out: Callable[[str], None]) -> int:
    """注册开机自启；被系统拒绝时主动提出提权重试。"""
    from . import bootstart

    try:
        password = ask("输入当前 Windows 账户密码（只交给任务计划程序保存）: ")
    except (EOFError, KeyboardInterrupt):
        return 2
    result = bootstart.install(port=2268, mode="task", password=password)
    out(bootstart.activate(result))
    if result.installed:
        return 0
    if "管理员权限" in result.detail:
        out("注册这种任务需要管理员权限（系统会拒绝为非管理员账户保存凭据）。")
        answer = ask("现在提权重试吗？会弹出 UAC 窗口 (y/N) ").strip().lower()
        if answer == "y":
            return _relaunch_elevated(["bootstart", "install", "--mode", "task"])
    return 1


def run_menu(
    ask: Callable[[str], str] = input, out: Callable[[str], None] = print
) -> int:
    """主循环：输入数字执行对应命令，0 退出。ask/out 可注入，便于测试。"""
    from . import cli

    out("校园网自连 · 主菜单")
    while True:
        for index, item in enumerate(ITEMS, start=1):
            hint = f"    {item.hint}" if item.hint else ""
            out(f"  {index}. {item.label}{hint}")
        out("  0. 退出")
        try:
            choice = ask("请输入序号: ").strip()
        except (EOFError, KeyboardInterrupt):
            out("")
            return 0
        if choice in ("0", "", "q", "quit"):
            return 0
        if not choice.isdigit() or not 1 <= int(choice) <= len(ITEMS):
            out("输入无效，请重新选择。")
            continue
        index = int(choice)
        item = ITEMS[index - 1]
        out("")
        if index == REGISTER_INDEX:
            register_bootstart(ask, out)
        else:
            cli.main(list(item.args))
        out("")
        try:
            ask("按回车返回菜单...")
        except (EOFError, KeyboardInterrupt):
            return 0
