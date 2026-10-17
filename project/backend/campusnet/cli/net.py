"""网络侧命令：立即检测、门户发现、环境自检、门户规则与门户报告。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys

from .. import discovery as discovery_mod
from ..monitor import MonitorManager
from ..paths import stdout_ready
from ..storage import Store


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


_WIN_SAFE_ARG = re.compile(r"^[\w@+=:,./\\-]+$", re.UNICODE)


def _quote_arg(part: str) -> str:
    """单个参数按当前 shell 的规则加引号。

    Windows 上不能用 subprocess.list2cmdline：它只管 argv 拆分，不管 shell
    元字符——`http://x/y?a=1&b=2` 里没有空格，它原样输出，cmd 会把 `&` 当成
    命令分隔符，后半截命令就丢了。这里对含 `& | < > ^ ( ) % ! "` 或空白的
    参数一律加双引号，内层引号按 CommandLineToArgvW 的规则反斜杠转义。
    """
    if os.name == "nt":
        if _WIN_SAFE_ARG.match(part):
            return part
        return '"' + part.replace('"', '\\"') + '"'
    return shlex.quote(part)


def _shell_line(parts: list[str]) -> str:
    """把参数拼成一条能直接粘贴的命令。

    不能手拼空格：`<学号>`、`<密码>` 这种占位符不加引号时，cmd / PowerShell /
    bash 都会把它当输入重定向，粘贴后报的是「找不到指定文件」。
    """
    return " ".join(_quote_arg(part) for part in parts)


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
    parts = [
        "python",
        "utils/run.py",
        "add",
        "--name",
        "我的校园网",
        "--portal",
        fields["portal_url"],
        "--username",
        "<学号>",
        "--password",
        "<密码>",
        "--password-mode",
        fields["password_mode"],
        "--login-mode",
        fields["login_mode"],
    ]
    print("  " + _shell_line(parts))
    if fields["username_field"]:
        print(
            "  追加："
            + _shell_line(
                [
                    "--username-field",
                    fields["username_field"],
                    "--password-field",
                    fields["password_field"],
                ]
            )
        )
    if fields["extra_fields"]:
        extra = json.dumps(fields["extra_fields"], ensure_ascii=False)
        print("  追加：" + _shell_line(["--extra", extra]))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """一键自检：本地状态 +（默认）真实探活，用于人在外面排查。"""
    stdout_ready()

    if args.power:
        from .. import powerprep as powerprep_mod

        items = powerprep_mod.run(fix=False)
        print(powerprep_mod.render(items, as_json=args.json))
        return powerprep_mod.exit_code(items)

    from .. import doctor as doctor_mod
    from ..logging_setup import setup_logging

    setup_logging()
    checks = doctor_mod.run(probe=not args.no_probe)
    print(doctor_mod.render(checks, as_json=args.json))
    return doctor_mod.exit_code(checks)


def cmd_profiles(args: argparse.Namespace) -> int:
    """查看 / 校验门户适配规则（新增一所学校 = 加一条数据）。"""
    stdout_ready()
    from pathlib import Path

    from .. import profiles as profiles_mod

    directory = Path(args.dir).expanduser() if args.dir else None
    items, errors = profiles_mod.validate_all(directory)
    if args.validate:
        for item in items:
            print(f"[OK  ] {item.id:<16} {item.name}（{item.source}）")
        for problem in errors:
            print(f"[FAIL] {problem}", file=sys.stderr)
        return 1 if errors else 0
    for item in items:
        tokens = ", ".join(item.tokens[:4]) or "（无特征）"
        print(
            f"{item.id:<16}{item.name:<14}{item.login_mode:<8}"
            f"{item.password_mode:<10}{tokens}"
        )
    for problem in errors:
        print(f"[WARN] {problem}", file=sys.stderr)
    return 0


def cmd_portal_report(args: argparse.Namespace) -> int:
    """导出一份已脱敏的门户 fixture，或对已有 fixture 做离线回放。"""
    stdout_ready()
    from pathlib import Path

    from .. import fixtures as fixtures_mod

    if args.replay:
        info = fixtures_mod.replay(args.replay)
        for key, value in info.items():
            print(f"{key}: {value}")
        return 0

    if args.from_file:
        source = Path(args.from_file)
        try:
            html = source.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"读不到 {source}：{exc}", file=sys.stderr)
            return 2
        url = args.url
    elif args.url:
        url = args.url
        try:
            html = fixtures_mod.fetch_page(url)
        except Exception as exc:  # noqa: BLE001 - 抓不到就如实报，不吞异常
            print(f"抓取门户失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    else:
        print("请给出 --url（未认证状态下抓取）或 --from-file（本地保存的 HTML）", file=sys.stderr)
        return 2

    clean = fixtures_mod.sanitize_html(html)
    target = Path(args.out) if args.out else Path("_tmp") / "portal-report.html"
    fixtures_mod.write_fixture(
        fixtures_mod.Fixture(html=clean, url=url), target
    )
    print(f"已写出脱敏 fixture：{target}")
    print("（已删除输入值、脚本正文、注释、URL 查询串、学号、手机号、内网地址与长令牌）")
    info = fixtures_mod.replay(target)
    print(f"识别结果：厂商={info['vendor']} 账号框={info['username_field'] or '（未识别）'}"
          f" 密码框={info['password_field'] or '（未识别）'}"
          f" 登录方式建议={info['suggested_login_mode']}"
          f" 验证码={'有' if info['needs_captcha'] else '无'}")
    print("这份文件可以直接贴到 issue / PR，用来补一条学校规则。")
    return 0
