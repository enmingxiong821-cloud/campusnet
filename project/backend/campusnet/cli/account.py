"""账号与凭据：添加/查看账号、服务令牌、口令文件、凭据导出导入。"""

from __future__ import annotations

import argparse
import json
import sys

from ..paths import stdout_ready
from ..storage import Account, Store


def cmd_add(args: argparse.Namespace) -> int:
    stdout_ready()

    # --show-ssid 必须早于 Store()：Store 构造会 mkdir 并建表，
    # 而这是个纯诊断开关，用户在「还没决定配不配 SSID」时就想看一眼当前
    # 连的是什么网，不该为此先在磁盘上落一个数据库文件。
    if getattr(args, "show_ssid", False):
        from .. import wifi as wifi_mod

        print(f"当前 Wi-Fi：{wifi_mod.current_ssid() or '(未连接无线网)'}")
        return 0

    # 补回 argparse 层撤掉的必填校验。SystemExit(2) 与 argparse 自身的
    # 参数错误保持同一个退出码，脚本里的 set -e 判断才不会失效。
    missing = [
        flag
        for flag, value in (
            ("--name", args.name),
            ("--portal", args.portal),
            ("--username", args.username),
            ("--password", args.password),
        )
        if not value
    ]
    if missing:
        print(
            f"campusnet add: error: the following arguments are required: {', '.join(missing)}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    store = Store()
    extra: dict[str, str] = {}
    if args.extra:
        try:
            extra = json.loads(args.extra)
        except json.JSONDecodeError as exc:
            print(f"--extra 不是合法 JSON: {exc}", file=sys.stderr)
            return 2

    # wifi_ssid 走 extra_fields 而不是 Account 新字段：
    # schema 已定型，加字段要动建表与迁移，而 SSID 本就是零散配置。
    # 显式 --extra 优先，留给需要同时改多个键的场景。
    if args.wifi_ssid:
        extra.setdefault("wifi_ssid", args.wifi_ssid)

    account = Account(
        name=args.name,
        portal_url=args.portal,
        probe_url=args.probe,
        username=args.username,
        password=args.password,
        username_field=args.username_field,
        password_field=args.password_field,
        password_mode=args.password_mode,
        login_mode=args.login_mode,
        extra_fields=extra,
        max_retries=args.max_retries,
        enabled=not args.disabled,
    )
    new_id = store.add_account(account)
    store.add_event(f"命令行新增账号 {account.name}", level="info", stage="cli", account_id=new_id)
    print(f"已添加账号 #{new_id} {account.name}")
    return 0


def cmd_list(_: argparse.Namespace) -> int:
    stdout_ready()
    store = Store()
    accounts = store.list_accounts()
    if not accounts:
        print("还没有账号，用 `python -m campusnet add` 添加。")
        return 0
    print(f"{'ID':<4}{'名称':<16}{'状态':<12}{'登录方式':<10}门户地址")
    for acc in accounts:
        print(
            f"{acc.id:<4}{acc.name:<16}{acc.last_state:<12}{acc.login_mode:<10}{acc.portal_url}"
        )
    return 0


def cmd_token(_: argparse.Namespace) -> int:
    """打印局域网控制台令牌，不把令牌放进 URL 或数据库。"""
    stdout_ready()
    from ..auth import ApiAuth

    print(ApiAuth().token)
    return 0


def _read_passphrase(path: str, prompt: str, *, confirm: bool = False) -> str:
    """取口令：优先从文件读（脚本/无人值守用），否则交互输入。"""
    from pathlib import Path

    if path:
        try:
            # utf-8-sig：Windows 记事本/PowerShell 写出来的口令文件常带 BOM，
            # 用 utf-8 读会把 BOM 当成口令的第一个字符，导致「口令不对」。
            value = Path(path).read_text(encoding="utf-8-sig").strip()
        except OSError as exc:
            print(f"读不到口令文件 {path}：{exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        if not value:
            print(f"口令文件 {path} 是空的。", file=sys.stderr)
            raise SystemExit(2)
        return value

    from getpass import getpass

    first = getpass(prompt)
    if not first:
        print("口令不能为空，已取消。", file=sys.stderr)
        raise SystemExit(2)
    if confirm and getpass("再输一次确认：") != first:
        print("两次输入不一致，已取消。", file=sys.stderr)
        raise SystemExit(2)
    return first


def cmd_cred(args: argparse.Namespace) -> int:
    """导出/导入账号凭据，用于换机或重装系统。"""
    stdout_ready()
    from pathlib import Path

    from .. import credentials as cred_mod

    if args.cred_action == "export":
        store = Store()
        accounts = store.list_accounts()
        if not accounts:
            print("还没有账号，没什么可导出的。")
            return 2
        target = Path(args.out)
        if target.exists() and not args.force:
            print(f"{target} 已存在；确认覆盖请加 --force。", file=sys.stderr)
            return 2
        passphrase = _read_passphrase(
            args.passphrase_file, "设置导出密码（迁移时要用它导入）：", confirm=True
        )
        try:
            envelope = cred_mod.export_accounts(store, passphrase)
            cred_mod.write_export(envelope, target)
        except (cred_mod.ExportError, OSError) as exc:
            print(f"导出失败：{exc}", file=sys.stderr)
            return 2
        print(f"已导出 {len(accounts)} 个账号到 {target}")
        print("提示：该文件等价于明文口令，迁移完请删除或妥善保管。")
        return 0

    source = Path(args.file)
    try:
        envelope = cred_mod.read_export(source)
    except cred_mod.ExportError as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 2
    passphrase = _read_passphrase(args.passphrase_file, "输入导出时设置的口令：")
    try:
        report = cred_mod.import_accounts(
            Store(), envelope, passphrase, dry_run=args.dry_run
        )
    except (cred_mod.ExportError, OSError) as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 2
    print(report.summary())
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """一键配置：检查环境、生成快捷方式、自检，并告诉你下一步。"""
    stdout_ready()
    from ..setup_flow import run_setup

    report = run_setup(
        with_browser=args.with_browser, make_shortcuts=not args.no_shortcut
    )
    print(report.render())
    return 0 if report.ok else 1
