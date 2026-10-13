"""系统集成：事件查询、开机自启、通知、多端访问、快捷方式、交互菜单。"""

from __future__ import annotations

import argparse
import sys

from ..paths import stdout_ready
from ..storage import Store


def cmd_events(args: argparse.Namespace) -> int:
    stdout_ready()
    store = Store()
    for event in store.list_events(limit=args.limit):
        print(f"{event['ts']} [{event['level']:<7}] {event['stage']:<12} {event['message']}")
    return 0


def cmd_bootstart(args: argparse.Namespace) -> int:
    """开机自启动的查看 / 安装 / 卸载。

    安装或卸载操作失败返回 1；未安装时卸载为幂等成功，返回 0。
    状态查询只展示当前结果，不触发系统安装或卸载操作。
    """
    stdout_ready()
    from .. import bootstart as bootstart_mod

    if args.bootstart_action == "status":
        result = bootstart_mod.status()
        print(bootstart_mod.activate(result))
        if result.detail:
            print(f"说明: {result.detail}")
        if result.path:
            print(f"启动项位置: {result.path}")
        return 0

    if args.bootstart_action == "verify":
        ok, detail = bootstart_mod.verify()
        print(("已验证生效：" if ok else "未证实生效：") + detail)
        return 0 if ok else 1

    if args.bootstart_action == "install":
        password = ""
        if args.mode == "task":
            from getpass import getpass

            password = getpass("输入当前 Windows 账户密码（只交给任务计划程序保存）：")
        result = bootstart_mod.install(
            host=args.host, port=args.port, mode=args.mode, password=password
        )
        print(bootstart_mod.activate(result))
        if not result.installed:
            return 1
        if bootstart_mod.platform_name() == "linux" and result.path and result.path.suffix == ".service":
            print("提示：默认随用户会话启动；无需登录运行需另行配置 linger，并确认系统授权策略。")
        if args.mode == "task":
            print("提示：任务会在开机时运行，不需要任何人登录；密码只由任务计划程序保存。")
        return 0

    result = bootstart_mod.uninstall()
    print(bootstart_mod.activate(result))
    return 0 if result.success else 1


def cmd_notify(args: argparse.Namespace) -> int:
    """发一条测试通知，用来确认 webhook 配置真的能通。"""
    stdout_ready()
    from ..notify import Notifier, send_test

    notifier = Notifier.from_store(Store())
    if args.url:
        notifier.url = args.url.strip()
    if not notifier.enabled:
        print(
            "未配置 webhook：请设置 notify_url（Web 控制台设置页），"
            "或用 --url / CAMPUSNET_NOTIFY_URL 临时指定。"
        )
        return 2
    ok = send_test(notifier)
    print(("测试通知已发送：" if ok else "测试通知发送失败：") + notifier.url)
    return 0 if ok else 1


def cmd_access(args: argparse.Namespace) -> int:
    """打印手机 / 平板访问控制台需要的信息，或输出远程接入三步向导。"""
    stdout_ready()
    from .. import access as access_mod

    if args.guide:
        for line in access_mod.guide(host=args.host, port=args.port):
            print(line)
        return 0
    for line in access_mod.report(host=args.host, port=args.port):
        print(line)
    return 0


def cmd_shortcut(args: argparse.Namespace) -> int:
    """在桌面生成每种用法的快捷方式。"""
    stdout_ready()
    from .. import shortcuts as shortcuts_mod

    ok, message = shortcuts_mod.create(
        args.dir or None, platform=args.platform or None
    )
    if ok:
        print(message)
        return 0
    print(f"创建快捷方式失败：{message}", file=sys.stderr)
    return 1


def cmd_menu(_: argparse.Namespace) -> int:
    """交互式主菜单：桌面快捷方式指向这里。"""
    stdout_ready()
    from ..menu import run_menu

    return run_menu()
