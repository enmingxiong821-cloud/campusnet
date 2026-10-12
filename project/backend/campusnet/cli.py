"""命令行入口：服务、添加账号、立即检测。"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from . import discovery as discovery_mod
from .monitor import MonitorManager
from .paths import API_HOST, API_PORT, stdout_ready
from .storage import Account, Store


def cmd_serve(args: argparse.Namespace) -> int:
    from .logging_setup import setup_logging
    from .server import serve

    target = setup_logging()
    if target is not None:
        print(f"运行日志：{target}")
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

    # --show-ssid 必须早于 Store()：Store 构造会 mkdir 并建表，
    # 而这是个纯诊断开关，用户在「还没决定配不配 SSID」时就想看一眼当前
    # 连的是什么网，不该为此先在磁盘上落一个数据库文件。
    if getattr(args, "show_ssid", False):
        from . import wifi as wifi_mod

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


def cmd_check(args: argparse.Namespace) -> int:
    stdout_ready()
    store = Store()
    manager = MonitorManager()
    monitor = manager.ensure(store)
    monitor.headless = not args.headful

    if args.account:
        account = store.get_account(args.account)
        if account is None:
            print(f"账号 #{args.account} 不存在", file=sys.stderr)
            return 2
        # 必须走 monitor.check_account，不能直接调 login()。
        # 直接调会绕过网络身份检查（SSID 是不是校园网）与门户会话复核：
        # 用户按 README 走单账号排障时，正在热点上这类问题一个都看不到，
        # 而全量 check 能看到——同一个账号两条路给出不同结论。
        result = monitor.check_account(account)
        store.add_event(
            f"CLI 检测 {account.name}: {result.state} - {result.message}",
            level="info" if result.online else "warning",
            stage="cli",
            state=result.state,
            account_id=account.id,
        )
        mark = "OK " if result.online else "FAIL"
        print(f"[{mark}] {result.account_name}: {result.state} - {result.message} ({result.elapsed}s)")
        return 0 if result.online else 1

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


def cmd_token(_: argparse.Namespace) -> int:
    """打印局域网控制台令牌，不把令牌放进 URL 或数据库。"""
    stdout_ready()
    from .auth import ApiAuth

    print(ApiAuth().token)
    return 0


def cmd_bootstart(args: argparse.Namespace) -> int:
    """开机自启动的查看 / 安装 / 卸载。

    安装或卸载操作失败返回 1；未安装时卸载为幂等成功，返回 0。
    状态查询只展示当前结果，不触发系统安装或卸载操作。
    """
    stdout_ready()
    from . import bootstart as bootstart_mod

    if args.bootstart_action == "status":
        result = bootstart_mod.status()
        print(bootstart_mod.activate(result))
        if result.path:
            print(f"启动项位置: {result.path}")
        return 0

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


def cmd_doctor(args: argparse.Namespace) -> int:
    """一键自检：本地状态 +（默认）真实探活，用于人在外面排查。"""
    stdout_ready()
    from . import doctor as doctor_mod
    from .logging_setup import setup_logging

    setup_logging()
    checks = doctor_mod.run(probe=not args.no_probe)
    print(doctor_mod.render(checks, as_json=args.json))
    return doctor_mod.exit_code(checks)


def cmd_notify(args: argparse.Namespace) -> int:
    """发一条测试通知，用来确认 webhook 配置真的能通。"""
    stdout_ready()
    from .notify import Notifier, send_test

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

    from . import credentials as cred_mod

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


def cmd_power(args: argparse.Namespace) -> int:
    """关机 / 重启 / 发 WOL 唤醒包。"""
    stdout_ready()
    from . import power as power_mod

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


def cmd_console(args: argparse.Namespace) -> int:
    """在默认浏览器里打开控制台。"""
    stdout_ready()
    import webbrowser

    url = f"http://{args.host}:{args.port}"
    opened = webbrowser.open(url)
    print(f"已在浏览器打开 {url}" if opened else f"未能自动打开，请手动访问 {url}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    """打印运行日志的位置与最近若干行。"""
    stdout_ready()
    from .logging_setup import log_path

    target = log_path()
    print(f"日志文件：{target}")
    if not target.exists():
        print("还没有日志文件；跑过 serve 之后才会有。")
        return 0
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-max(1, int(args.lines)) :]))
    return 0


def cmd_access(args: argparse.Namespace) -> int:
    """打印手机 / 平板访问控制台需要的信息。"""
    stdout_ready()
    from . import access as access_mod

    for line in access_mod.report(host=args.host, port=args.port):
        print(line)
    return 0


def cmd_shortcut(args: argparse.Namespace) -> int:
    """在桌面生成每种用法的快捷方式。"""
    stdout_ready()
    from . import shortcuts as shortcuts_mod

    ok, message = shortcuts_mod.create(
        args.dir or None, platform=args.platform or None
    )
    if ok:
        print(message)
        return 0
    print(f"创建快捷方式失败：{message}", file=sys.stderr)
    return 1


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
    # 这四项不在 argparse 层设 required=True：--show-ssid 是个纯诊断开关，
    # 用户只想看一眼当前连的是什么网，不该被逼着提供账号密码。
    # 必填校验下移到 cmd_add，仍走 parser.error()，退出码保持 2 不变。
    p_add.add_argument("--name", default="", help="账号备注名")
    p_add.add_argument("--portal", default="", help="认证门户地址")
    p_add.add_argument("--username", default="", help="认证账号")
    p_add.add_argument("--password", default="", help="认证密码")
    p_add.add_argument("--probe", default="http://www.msftconnecttest.com/connecttest.txt")
    p_add.add_argument("--username-field", default="", help="门户表单里的账号字段名（留空自动识别）")
    p_add.add_argument("--password-field", default="", help="门户表单里的密码字段名（留空自动识别）")
    p_add.add_argument(
        "--password-mode",
        default="plain",
        choices=["plain", "base64", "md5", "md5_base64", "sha256"],
        help="密码在提交前的编码方式",
    )
    p_add.add_argument(
        "--login-mode",
        default="http",
        choices=["http", "browser", "manual", "srun"],
        help="http=表单登录；browser=真浏览器；srun=深澜challenge 加密；manual=只检测不登录",
    )
    p_add.add_argument("--extra", default="", help='附加字段 JSON，如 \'{"upass":"1"}\'')
    p_add.add_argument(
        "--wifi-ssid",
        default="",
        help=(
            "校园网 Wi-Fi 名，多个用逗号分隔。不配则跳过网络身份检查；"
            "配了以后连错网（如手机热点）会被识别出来并告警——"
            "热点同样放行探活地址，不检查的话程序会误以为已联网"
        ),
    )
    p_add.add_argument(
        "--show-ssid",
        action="store_true",
        help="打印当前连接的无线路由名，便于照着填 --wifi-ssid",
    )
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

    p_token = sub.add_parser("token", help="查看局域网 Web 控制台令牌")
    p_token.set_defaults(func=cmd_token)

    p_boot = sub.add_parser("bootstart", help="查看 / 安装 / 卸载开机自启动")
    boot_sub = p_boot.add_subparsers(dest="bootstart_action", required=True)
    p_boot_status = boot_sub.add_parser("status", help="查看当前是否已注册开机自启动")
    p_boot_status.set_defaults(func=cmd_bootstart)
    p_boot_install = boot_sub.add_parser("install", help="注册开机自启动")
    p_boot_install.add_argument("--host", default=API_HOST)
    p_boot_install.add_argument("--port", type=int, default=API_PORT)
    p_boot_install.add_argument(
        "--mode",
        choices=["auto", "task"],
        default="auto",
        help=(
            "auto=平台默认载体（Windows 为「登录后」启动）；"
            "task=Windows 计划任务（开机触发，无需登录即可认证）"
        ),
    )
    p_boot_install.set_defaults(func=cmd_bootstart)
    p_boot_uninstall = boot_sub.add_parser("uninstall", help="移除开机自启动")
    p_boot_uninstall.set_defaults(func=cmd_bootstart)

    p_doctor = sub.add_parser(
        "doctor", help="一键自检：端口、数据库、令牌、自启动、探活"
    )
    p_doctor.add_argument(
        "--no-probe", action="store_true", help="跳过真实探活，只查本地状态"
    )
    p_doctor.add_argument("--json", action="store_true", help="输出 JSON，便于脚本判断")
    p_doctor.set_defaults(func=cmd_doctor)

    p_notify = sub.add_parser("notify", help="发送一条测试通知（校验 webhook 配置）")
    p_notify.add_argument("--url", default="", help="临时覆盖配置里的 webhook 地址")
    p_notify.set_defaults(func=cmd_notify)

    p_cred = sub.add_parser("cred", help="导出/导入账号凭据（换机、重装系统用）")
    cred_sub = p_cred.add_subparsers(dest="cred_action", required=True)
    p_cred_export = cred_sub.add_parser("export", help="导出口令保护的凭据文件")
    p_cred_export.add_argument("--out", default="campusnet-credentials.json")
    p_cred_export.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    p_cred_export.add_argument(
        "--passphrase-file", default="", help="从文件读口令（脚本/无人值守用）"
    )
    p_cred_export.set_defaults(func=cmd_cred)
    p_cred_import = cred_sub.add_parser("import", help="从凭据文件恢复账号")
    p_cred_import.add_argument("--file", required=True)
    p_cred_import.add_argument(
        "--dry-run", action="store_true", help="只报告会做什么，不写数据库"
    )
    p_cred_import.add_argument("--passphrase-file", default="")
    p_cred_import.set_defaults(func=cmd_cred)

    p_power = sub.add_parser("power", help="关机 / 重启 / 发送 WOL 唤醒包")
    power_sub = p_power.add_subparsers(dest="power_action", required=True)
    for action, action_help in (("off", "关机"), ("reboot", "重启")):
        p_power_action = power_sub.add_parser(action, help=action_help)
        p_power_action.add_argument("--delay", type=int, default=0, help="延迟秒数")
        p_power_action.add_argument(
            "--dry-run", action="store_true", help="只打印将要执行的命令"
        )
        p_power_action.set_defaults(func=cmd_power)
    p_power_wake = power_sub.add_parser("wake", help="发送 WOL 魔术包唤醒另一台机器")
    p_power_wake.add_argument("--mac", default="", help="目标网卡 MAC")
    p_power_wake.add_argument("--broadcast", default="255.255.255.255")
    p_power_wake.add_argument("--port", type=int, default=9)
    p_power_wake.set_defaults(func=cmd_power)

    p_console = sub.add_parser("console", help="在默认浏览器里打开控制台")
    p_console.add_argument("--host", default="127.0.0.1")
    p_console.add_argument("--port", type=int, default=API_PORT)
    p_console.set_defaults(func=cmd_console)

    p_logs = sub.add_parser("logs", help="查看运行日志的位置与最近内容")
    p_logs.add_argument("--lines", type=int, default=20)
    p_logs.set_defaults(func=cmd_logs)

    p_access = sub.add_parser("access", help="查看手机 / 平板访问控制台需要的信息")
    p_access.add_argument("--host", default=API_HOST)
    p_access.add_argument("--port", type=int, default=API_PORT)
    p_access.set_defaults(func=cmd_access)

    p_shortcut = sub.add_parser("shortcut", help="在桌面生成每种用法的快捷方式")
    p_shortcut.add_argument("--dir", default="", help="输出目录（默认桌面）")
    p_shortcut.add_argument(
        "--platform", default="", help="强制平台（测试用）：windows/darwin/linux"
    )
    p_shortcut.set_defaults(func=cmd_shortcut)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
