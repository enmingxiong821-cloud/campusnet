"""命令行入口：服务、添加账号、立即检测。"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from . import discovery as discovery_mod
from .monitor import MonitorManager
from .paths import API_HOST, API_PORT, stdout_ready
from .portal import login
from .storage import Account, Store


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    try:
        serve(
            host=args.host,
            port=args.port,
            autostart=not args.no_autostart,
            blocking=True,
            auto_shift=args.auto_shift,
        )
    except SystemExit:
        raise
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    stdout_ready()
    store = Store()
    extra: dict[str, str] = {}
    if args.extra:
        try:
            extra = json.loads(args.extra)
        except json.JSONDecodeError as exc:
            print(f"--extra 不是合法 JSON: {exc}", file=sys.stderr)
            return 2
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


def cmd_check(args: argparse.Namespace) -> int:
    stdout_ready()
    store = Store()
    if args.account:
        account = store.get_account(args.account)
        if account is None:
            print(f"账号 #{args.account} 不存在", file=sys.stderr)
            return 2
        result = login(account, headless=not args.headful)
        store.add_event(
            f"CLI 检测 {account.name}: {result.state} - {result.message}",
            level="info" if result.ok else "warning",
            stage="cli",
            state=result.state,
            account_id=account.id,
        )
        print(f"{account.name}: {result.state} - {result.message}")
        return 0 if result.ok else 1

    manager = MonitorManager()
    monitor = manager.ensure(store)
    monitor.headless = not args.headful
    results = monitor.check_all()
    if not results:
        print("没有启用中的账号。")
        return 0
    failed = 0
    for item in results:
        mark = "OK " if item["online"] else "FAIL"
        failed += 0 if item["online"] else 1
        print(f"[{mark}] {item['account_name']}: {item['state']} - {item['message']} ({item['elapsed']}s)")
    return 0 if failed == 0 else 1


def cmd_events(args: argparse.Namespace) -> int:
    stdout_ready()
    store = Store()
    for event in store.list_events(limit=args.limit):
        print(f"{event['ts']} [{event['level']:<7}] {event['stage']:<12} {event['message']}")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    stdout_ready()
    report = discovery_mod.discover(
        seed=args.seed or "",
        timeout=args.timeout,
        include_gateway_scan=not args.no_scan,
    )
    print(f"探活状态: {report['probe_status']} ({report['probe_detail'][:60]})")
    if report["redirect_portal"]:
        print(f"跳转到门户: {report['redirect_portal']}")
    if report["gateways"]:
        print(f"候选网关: {', '.join(report['gateways'][:4])}")

    candidates = report["candidates"]
    if not candidates:
        for hint in report["hints"]:
            print(f"  ! {hint}")
        return 1

    print(f"\n找到 {len(candidates)} 个门户候选：")
    for index, item in enumerate(candidates, start=1):
        print(f"\n[{index}] {item['vendor']}  HTTP {item['status']}")
        print(f"    地址   : {item['url']}")
        if item["title"]:
            print(f"    标题   : {item['title']}")
        print(f"    账号框 : {item['username_field'] or '未识别'}")
        print(f"    密码框 : {item['password_field'] or '未识别'}")
        print(f"    提交路径: {item['login_path'] or '(同页)'}")
        print(f"    建议方式: login_mode={item['suggested_login_mode']} password_mode={item['suggested_password_mode']}")
        if item["needs_captcha"]:
            print("    验证码 : 有（必须用浏览器登录）")
        if item["extra_fields"]:
            print(f"    隐藏域 : {', '.join(item['extra_fields'])}")

    best = candidates[0]
    fields = discovery_mod.to_account_fields(best)
    print("\n一键建号命令（用最佳候选）：")
    print(
        "  python utils/run.py add --name 我的校园网"
        f" --portal {fields['portal_url']}"
        f" --username <学号> --password <密码>"
        f" --password-mode {fields['password_mode']}"
        f" --login-mode {fields['login_mode']}"
    )
    if fields["username_field"]:
        print(f"  追加：--username-field {fields['username_field']}"
              f" --password-field {fields['password_field']}")
    if fields["extra_fields"]:
        extra = json.dumps(fields["extra_fields"], ensure_ascii=False)
        print(f"  追加：--extra '{extra}'")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="campusnet", description="校园网自连：浏览器验证式自动重连工具"
    )
    parser.add_argument("--version", action="version", version=f"campusnet {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="启动 Web 控制台与自动监控")
    p_serve.add_argument("--host", default=API_HOST)
    p_serve.add_argument("--port", type=int, default=API_PORT)
    p_serve.add_argument(
        "--no-autostart", action="store_true", help="启动服务但不开启后台监控"
    )
    p_serve.add_argument(
        "--auto-shift",
        action="store_true",
        help="端口被占时自动往后找一个空闲端口",
    )
    p_serve.set_defaults(func=cmd_serve)

    p_add = sub.add_parser("add", help="添加一个校园网账号")
    p_add.add_argument("--name", required=True, help="账号备注名")
    p_add.add_argument("--portal", required=True, help="认证门户地址")
    p_add.add_argument("--username", required=True, help="认证账号")
    p_add.add_argument("--password", required=True, help="认证密码")
    p_add.add_argument("--probe", default="http://www.msftconnecttest.com/connecttest.txt")
    p_add.add_argument("--username-field", default="", help="门户表单里的账号字段名（留空自动识别）")
    p_add.add_argument("--password-field", default="", help="门户表单里的密码字段名（留空自动识别）")
    p_add.add_argument(
        "--password-mode",
        default="plain",
        choices=["plain", "base64", "md5", "md5_base64", "sha256"],
        help="密码在提交前的编码方式",
    )
    p_add.add_argument("--login-mode", default="http", choices=["http", "browser", "manual"])
    p_add.add_argument("--extra", default="", help='附加字段 JSON，如 \'{"upass":"1"}\'')
    p_add.add_argument("--max-retries", type=int, default=3)
    p_add.add_argument("--disabled", action="store_true")
    p_add.set_defaults(func=cmd_add)

    p_discover = sub.add_parser("discover", help="自动发现当前网络的门户地址与字段")
    p_discover.add_argument("--seed", default="", help="探测种子地址，留空用默认探活地址")
    p_discover.add_argument("--timeout", type=float, default=4.0)
    p_discover.add_argument("--no-scan", action="store_true", help="不扫描网关，只看种子地址")
    p_discover.set_defaults(func=cmd_discover)

    p_list = sub.add_parser("list", help="列出已配置账号")
    p_list.set_defaults(func=cmd_list)

    p_check = sub.add_parser("check", help="立即探活（必要时自动登录）")
    p_check.add_argument("--account", type=int, default=0, help="指定账号 ID，省略则检查全部")
    p_check.add_argument("--headful", action="store_true", help="浏览器模式下显示窗口")
    p_check.set_defaults(func=cmd_check)

    p_events = sub.add_parser("events", help="查看最近事件日志")
    p_events.add_argument("--limit", type=int, default=50)
    p_events.set_defaults(func=cmd_events)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())