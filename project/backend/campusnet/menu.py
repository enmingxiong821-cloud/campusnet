"""交互式主菜单：桌面上只留一个快捷方式，程序能做的事都在这里。

为什么这么做：把「启动服务 / 打开控制台 / 检测 / 建号 / 自检 / 看日志 / 开关机 /
注册自启 / 配通知 / 装浏览器依赖 / 刷新快捷方式」拆成十几个快捷方式，桌面很乱，
用户还得记住哪个是哪个。合并成一个入口后，需要的只是按一次数字键。
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

Ask = Callable[[str], str]
Out = Callable[[str], None]


@dataclass(frozen=True)
class MenuItem:
    label: str
    hint: str = ""
    section: str = ""
    args: tuple[str, ...] = ()
    handler: Callable[[Ask, Out], int] | None = None


def _relaunch_elevated(args: list[str]) -> int:
    """用 UAC 提权重跑自己：注册「无需登录」的任务必须管理员权限。"""
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return 1
    from .bootstart import launch_command_line
    from .shortcuts import elevate_command_line

    # -Wait：等提权后的那次注册真正跑完，菜单里才能紧接着报告结果。
    script = elevate_command_line(launch_command_line(args)) + " -Wait"
    try:
        subprocess.run([exe, "-NoProfile", "-Command", script], timeout=600)
    except (OSError, subprocess.SubprocessError):
        return 1
    return 0


def register_bootstart(ask: Ask, out: Out) -> int:
    """注册开机自启；被系统拒绝时主动提出提权重试。"""
    from getpass import getpass

    from . import bootstart

    try:
        # 走 getpass 而不是注入的 ask：菜单是双击桌面图标进来的，屏幕后面可能
        # 还坐着别人。同一个密码在命令行的 bootstart 路径里用的就是 getpass。
        password = getpass("输入当前 Windows 账户密码（不回显，只交给任务计划程序保存）: ")
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


def guided_add(ask: Ask, out: Out) -> int:
    """发现门户并引导建号：不想碰命令行的用户走这条路。"""
    from getpass import getpass

    from . import discovery as discovery_mod
    from .storage import Account, Store

    out("正在发现门户（掉线状态下最准，可能需要十几秒）…")
    try:
        report = discovery_mod.discover()
    except Exception as exc:  # noqa: BLE001 - 发现失败不该让菜单崩掉
        out(f"发现问题失败：{type(exc).__name__}: {exc}")
        return 1
    candidates = report.get("candidates") or []
    if not candidates:
        out("没发现可用门户。先在浏览器里打开认证页，再执行这条投稿一份脱敏样本：")
        out("  python utils/run.py portal-report --url <门户地址>")
        return 1
    first = candidates[0]
    candidate = first if isinstance(first, dict) else first.as_dict()
    out(f"最佳候选：{candidate.get('vendor', '未知厂商')} / {candidate.get('url', '')}")
    name = ask("账号备注名（回车用「我的校园网」）: ").strip() or "我的校园网"
    username = ask("学号 / 账号: ").strip()
    if not username:
        out("没填账号，已取消。")
        return 2
    password = getpass("密码（不回显，也不会进 shell 历史）: ")
    if not password:
        out("没填密码，已取消。")
        return 2
    fields = discovery_mod.to_account_fields(candidate)
    store = Store()
    new_id = store.add_account(
        Account(name=name, username=username, password=password, **fields)
    )
    store.add_event(
        f"菜单添加账号 {name}", level="info", stage="account", account_id=new_id
    )
    out(f"已添加账号 #{new_id}（登录方式 {fields.get('login_mode', 'http')}）。")
    out("建议接着选「立即检测网络」验证一次。")
    return 0


def configure_notify(ask: Ask, out: Out) -> int:
    """配置失败通知的 webhook，并当场发一条测试。"""
    from .notify import Notifier, send_test
    from .storage import Store

    store = Store()
    current = Notifier.from_store(store)
    out(f"当前 Webhook：{current.url or '（未配置）'}")
    url = ask("粘贴 webhook 地址（回车取消）: ").strip()
    if not url:
        return 0
    store.set_setting("notify_url", url)
    out("已保存，正在发测试通知…")
    ok = send_test(Notifier.from_store(store))
    out("测试通知已发送。" if ok else "测试通知发送失败（配置已保存，可稍后重试）。")
    return 0 if ok else 1


def install_browser(ask: Ask, out: Out) -> int:
    """按需安装浏览器登录依赖（几百 MB，先问一句）。"""
    from .setup_flow import browser_ready, install_browser_deps

    if browser_ready():
        out("Playwright 已安装，无需重复安装。")
        return 0
    out("浏览器登录依赖会下载 Chromium（约几百 MB），只在验证码 / CAS 门户时需要。")
    if ask("现在安装吗？(y/N) ").strip().lower() != "y":
        return 0
    out("正在安装，可能需要几分钟…")
    ok, message = install_browser_deps()
    out(message)
    return 0 if ok else 1


def _power_handler(action: str, label: str) -> Callable[[Ask, Out], int]:
    """关机 / 重启菜单项：选中就直接执行，所以先问一句。

    `power off` 的 --delay 默认是 0，按错一个数字就是当场关机——而「人不在机器
    旁边」正是这个功能的使用场景，关错了开不回来。同文件里注册自启这种可逆操作
    都要确认，关机更不该例外。
    """

    def handler(ask: Ask, out: Out) -> int:
        from . import cli

        warn = "关机后要靠 WOL / 智能插座才能再开机。" if action == "off" else ""
        try:
            answer = ask(f"确认立即{label}？{warn}(y/N) ")
        except (EOFError, KeyboardInterrupt):
            out("\n已取消，未执行。")
            return 0
        if answer.strip().lower() not in ("y", "yes"):
            out("已取消，未执行。")
            return 0
        return cli.main(["power", action])

    return handler


ITEMS: tuple[MenuItem, ...] = (
    MenuItem("打开控制台（浏览器）", "看状态、改设置、点按钮", "启动与检测", ("console",)),
    MenuItem("启动服务", "前台运行，Ctrl+C 停止", "启动与检测", ("serve",)),
    MenuItem("立即检测网络", "掉线会自动重连", "启动与检测", ("check",)),
    MenuItem("添加账号", "自动发现门户并引导建号", "账号", handler=guided_add),
    MenuItem("查看多端访问", "手机该访问哪个地址、令牌多少", "账号", ("access",)),
    # 与上一条分开、而不是把 --guide 并进去：向导会拿掉令牌那一行，而令牌正是手机
    # 首次登录要用的。两件事各给一个入口，比一条命令按参数变脸更好懂。
    MenuItem(
        "远程接入向导",
        "Tailscale + 远程桌面 + 添加到主屏幕，三步走",
        "账号",
        ("access", "--guide"),
    ),
    MenuItem("查看自检报告", "含版本更新提示（联网时）", "状态", ("doctor",)),
    MenuItem("查看运行日志", "最近 40 行", "状态", ("logs", "--lines", "40")),
    # 体检排在关机前面：没确认过前置条件就关机，很可能再也开不回来。
    MenuItem("电源体检", "关机 / 唤醒的前置条件逐项检查", "电源与自启", ("power", "prep")),
    MenuItem(
        "远程关机", "关机后要靠 WOL / 插座再开机", "电源与自启",
        handler=_power_handler("off", "关机"),
    ),
    MenuItem("远程重启", "", "电源与自启", handler=_power_handler("reboot", "重启")),
    MenuItem(
        "注册开机自启", "开机即认证、无需登录（需要管理员）", "电源与自启",
        handler=register_bootstart,
    ),
    MenuItem("卸载开机自启", "", "电源与自启", ("bootstart", "uninstall")),
    MenuItem("配置失败通知", "填一个 webhook 地址并测试", "设置", handler=configure_notify),
    MenuItem("安装浏览器登录依赖", "只在验证码 / CAS 门户时需要", "设置", handler=install_browser),
    MenuItem("刷新桌面快捷方式", "合并为一个入口并清理旧名字", "设置", ("shortcut",)),
)


def index_of(keyword: str) -> int:
    """按关键词找菜单序号（从 1 开始），找不到返回 0。"""
    for position, item in enumerate(ITEMS, start=1):
        if keyword in item.label:
            return position
    return 0


def service_running(port: int | None = None, timeout: float = 0.5) -> bool:
    """本机服务在不在跑：连一下 /healthz。

    短超时是必须的——本机未监听的端口是丢包而不是立刻拒绝，不给超时菜单会卡在
    那儿，而「双击桌面图标半天没反应」比不报状态更糟。这个端点在免认证名单里，
    不需要令牌。
    """
    import urllib.error
    import urllib.request

    from .paths import API_PORT

    url = f"http://127.0.0.1:{API_PORT if port is None else port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= int(getattr(resp, "status", 0)) < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def _ago(text: str | None, now: datetime | None = None) -> str:
    """把库里存的时间戳说成「多久前」，与控制台顶栏的口径保持一致。"""
    from .wakeplan import parse_last_run

    when = parse_last_run(text)
    if when is None:
        return ""
    seconds = ((now or datetime.now()) - when).total_seconds()
    if seconds < 60:  # 也兜住时钟回拨出的负数，别显示「-3 分钟前」
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


def status_line(
    store: object | None = None,
    *,
    running: Callable[[], bool] | None = None,
    now: datetime | None = None,
) -> str:
    """进菜单先报一句现状：账号几个、最近一次检测在什么时候、服务在不在跑。

    双击桌面快捷方式进来的用户，在一列功能之前最想知道的正是这几件事；没有这一行，
    得先选「查看自检报告」才知道自己处在哪。读不到的项直接省略——菜单不能因为
    读库失败或端口探不通就起不来。
    """
    parts: list[str] = []
    try:
        from .storage import Store

        accounts = list((store or Store()).list_accounts())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 只影响这一行，不该拦住菜单
        accounts = None
    if accounts is not None:
        if not accounts:
            parts.append("还没有账号（选「添加账号」）")
        else:
            enabled = sum(1 for a in accounts if a.enabled)
            parts.append(f"账号 {len(accounts)} 个（启用 {enabled}）")
            newest = max(
                (a.last_checked_at for a in accounts if a.last_checked_at),
                default=None,
            )
            ago = _ago(newest, now)
            if ago:
                parts.append(f"最近检测 {ago}")
    try:
        alive = (running or service_running)()
    except Exception:  # noqa: BLE001
        alive = None
    if alive is not None:
        parts.append("服务运行中" if alive else "服务未启动（选「启动服务」）")
    return " · ".join(parts)


def run_menu(ask: Ask = input, out: Out = print) -> int:
    """主循环：输入数字执行对应动作，0 退出。ask/out 可注入，便于测试。"""
    from . import cli

    out("校园网自连 · 主菜单")
    summary = status_line()
    if summary:
        out(summary)
    section = ""
    while True:
        for position, item in enumerate(ITEMS, start=1):
            if item.section and item.section != section:
                section = item.section
                out(f"\n[{section}]")
            hint = f"    {item.hint}" if item.hint else ""
            out(f"  {position}. {item.label}{hint}")
        out("\n  0. 退出")
        try:
            choice = ask("请输入序号: ").strip()
        except (EOFError, KeyboardInterrupt):
            out("")
            return 0
        if choice in ("0", "", "q", "quit"):
            return 0
        section = ""  # 重新打印时再分节
        if not choice.isdigit() or not 1 <= int(choice) <= len(ITEMS):
            out("输入无效，请重新选择。")
            continue
        item = ITEMS[int(choice) - 1]
        out("")
        try:
            if item.handler is not None:
                code = item.handler(ask, out)
            else:
                code = cli.main(list(item.args))
        except SystemExit as exc:
            # 菜单里的动作会 SystemExit：`serve` 发现「已有实例在跑」或端口被占时
            # 就是这么退的。不接住的话整个菜单进程跟着结束——桌面快捷方式双击进来的
            # 窗口会直接关掉，用户连那句错误都来不及看，正是本模块开头说要避免的
            # 那种劝退点。接住之后仍如实报退出码。
            raw = exc.code
            code = raw if isinstance(raw, int) else (0 if raw is None else 1)
        # 2 在本项目里是「用户取消」（guided_add、register_bootstart 取消时返回它），
        # 不当失败报，否则每次按 Ctrl+C 取消都要多挨一句。
        if code and code != 2:
            out(f"（上一步没有成功，退出码 {code}）")
        out("")
        try:
            ask("按回车返回菜单...")
        except (EOFError, KeyboardInterrupt):
            return 0
