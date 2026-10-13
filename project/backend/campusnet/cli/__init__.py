"""命令行入口：服务、添加账号、立即检测。

按主题拆成 account / net / power / service / system 五个子模块，本文件只留
解析器与入口。`build_parser()` 用 `set_defaults(func=cmd_xxx)` 直接引用函数名，
所以下面这圈 import 不是装饰——名字必须在包命名空间里可见，少一个就启动即报错。
"""

from __future__ import annotations

import argparse

from .. import __version__
from ..paths import API_HOST, API_PORT
from .. import discovery as discovery_mod  # 调用方与测试按 cli.discovery_mod 取，勿删
from .account import cmd_add, cmd_cred, cmd_list, cmd_setup, cmd_token
from .net import cmd_check, cmd_discover, cmd_doctor, cmd_portal_report, cmd_profiles
from .power import cmd_power
from .service import cmd_console, cmd_logs, cmd_openapi, cmd_serve
from .system import (
    cmd_access,
    cmd_bootstart,
    cmd_events,
    cmd_menu,
    cmd_notify,
    cmd_shortcut,
)


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
    p_boot_verify = boot_sub.add_parser("verify", help="自证「无需登录即运行」是否真的生效")
    p_boot_verify.set_defaults(func=cmd_bootstart)

    p_doctor = sub.add_parser(
        "doctor", help="一键自检：端口、数据库、令牌、自启动、探活"
    )
    p_doctor.add_argument(
        "--no-probe", action="store_true", help="跳过真实探活，只查本地状态"
    )
    p_doctor.add_argument(
        "--power", action="store_true", help="改为电源体检（远程开关机前置条件）"
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

    p_power_prep = power_sub.add_parser(
        "prep", help="电源体检：远程开关机的前置条件逐项检查"
    )
    p_power_prep.add_argument("--fix", action="store_true", help="尝试自动修复可修复项")
    p_power_prep.add_argument(
        "--yes", action="store_true", help="配合 --fix，跳过确认直接执行"
    )
    p_power_prep.add_argument("--json", action="store_true", help="输出 JSON，便于脚本判断")
    p_power_prep.set_defaults(func=cmd_power)

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
    p_access.add_argument(
        "--guide",
        action="store_true",
        help="输出远程接入三步向导（含 Tailscale 状态检测）",
    )
    p_access.set_defaults(func=cmd_access)

    p_shortcut = sub.add_parser("shortcut", help="在桌面生成每种用法的快捷方式")
    p_shortcut.add_argument("--dir", default="", help="输出目录（默认桌面）")
    p_shortcut.add_argument(
        "--platform", default="", help="强制平台（测试用）：windows/darwin/linux"
    )
    p_shortcut.set_defaults(func=cmd_shortcut)

    p_setup = sub.add_parser("setup", help="一键配置：环境检查 + 快捷方式 + 自检")
    p_setup.add_argument(
        "--with-browser",
        action="store_true",
        help="顺带安装 Playwright 与 Chromium（仅浏览器登录模式需要，约几百 MB）",
    )
    p_setup.add_argument(
        "--no-shortcut", action="store_true", help="不生成桌面快捷方式"
    )
    p_setup.set_defaults(func=cmd_setup)

    p_menu = sub.add_parser("menu", help="交互式主菜单（桌面快捷方式指向它）")
    p_menu.set_defaults(func=cmd_menu)

    p_profiles = sub.add_parser("profiles", help="查看 / 校验门户适配规则")
    p_profiles.add_argument(
        "--dir",
        default="",
        help="只看某个规则目录（默认内置目录 + CAMPUSNET_PROFILES_DIR）",
    )
    p_profiles.add_argument("--validate", action="store_true", help="逐条校验规则文件")
    p_profiles.set_defaults(func=cmd_profiles)

    p_openapi = sub.add_parser("openapi", help="输出控制台 API 的 OpenAPI 契约")
    p_openapi.add_argument("--out", default="", help="写到文件（默认打印到终端）")
    p_openapi.set_defaults(func=cmd_openapi)

    p_report = sub.add_parser(
        "portal-report", help="导出门户 fixture（已脱敏）或离线回放"
    )
    p_report.add_argument("--url", default="", help="门户地址（未认证状态下抓取）")
    p_report.add_argument(
        "--from-file", default="", help="从本地保存的 HTML 生成 fixture（不联网）"
    )
    p_report.add_argument(
        "--out", default="", help="输出路径（默认 _tmp/portal-report.html）"
    )
    p_report.add_argument("--replay", default="", help="回放已有 fixture，不抓取也不写文件")
    p_report.set_defaults(func=cmd_portal_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
