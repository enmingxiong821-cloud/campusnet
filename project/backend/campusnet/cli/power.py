"""远程开关机：关机、重启、网络唤醒与电源体检。"""

from __future__ import annotations

import argparse
import sys

from ..paths import stdout_ready


def cmd_power(args: argparse.Namespace) -> int:
    """关机 / 重启 / 发 WOL 唤醒包 / 电源体检。"""
    stdout_ready()

    if args.power_action == "prep":
        from .. import powerprep as powerprep_mod

        if args.fix and not args.yes:
            # 会改系统设置（注册表 / powercfg），先只读预览再要一句确认。
            preview = powerprep_mod.run(fix=False)
            pending = [
                item
                for item in preview
                if item.fixable
                and item.status in (powerprep_mod.WARN, powerprep_mod.MANUAL)
            ]
            if not pending:
                print(powerprep_mod.render(preview, as_json=args.json))
                return powerprep_mod.exit_code(preview)
            print("将尝试自动修复：")
            for item in pending:
                print(f"  - {item.name}：{item.detail}")
            try:
                answer = input("确认执行？[y/N] ")
            except (EOFError, KeyboardInterrupt):
                # 没有终端时（双击快捷方式、计划任务、stdin 被重定向）input 直接抛
                # EOFError，Ctrl-C 抛 KeyboardInterrupt——以前这两种都会带着整条栈
                # 冒到用户面前。与 menu.py 的处理一致按「取消」算：读不到确认就等于
                # 没确认，绝不顺着往下走去改注册表和 powercfg。
                print("\n已取消，未做任何修改。")
                return 0
            if answer.strip().lower() not in ("y", "yes"):
                print("已取消，未做任何修改。")
                return 0
        items = powerprep_mod.run(fix=args.fix, yes=args.yes)
        print(powerprep_mod.render(items, as_json=args.json))
        return powerprep_mod.exit_code(items)

    from .. import power as power_mod

    try:
        if args.power_action == "wake":
            if not args.mac:
                print("唤醒需要 --mac，例如 --mac AA:BB:CC:DD:EE:FF", file=sys.stderr)
                return 2
            target = power_mod.wake(args.mac, args.broadcast, args.port)
            print(f"已向 {target} 发送唤醒包（需同一二层内的设备执行才有效）")
            return 0
        if args.power_action == "off":
            verb = "关机"
            command = power_mod.shutdown(args.delay, dry_run=args.dry_run)
        else:
            verb = "重启"
            command = power_mod.reboot(args.delay, dry_run=args.dry_run)
    except power_mod.PowerError as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1
    prefix = "[试运行] 将执行" if args.dry_run else "已下发"
    print(f"{prefix}{verb}指令：{command}")
    return 0
