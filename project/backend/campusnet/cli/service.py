"""服务进程：启动控制台服务、打开控制台、查看运行日志、导出 OpenAPI。"""

from __future__ import annotations

import argparse
import json

from ..paths import stdout_ready
from ..storage import Store


def cmd_serve(args: argparse.Namespace) -> int:
    from ..logging_setup import setup_logging
    from ..server import serve

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
    from ..logging_setup import log_path

    target = log_path()
    print(f"日志文件：{target}")
    if not target.exists():
        print("还没有日志文件；跑过 serve 之后才会有。")
        return 0
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-max(1, int(args.lines)) :]))
    return 0


def cmd_openapi(args: argparse.Namespace) -> int:
    """打印（或写出）控制台 API 的 OpenAPI 契约。"""
    stdout_ready()
    import json
    from pathlib import Path

    from .. import openapi as openapi_mod
    from .. import server as server_mod
    from ..monitor import MonitorManager

    store = Store()
    handler = server_mod.build_handler(store, MonitorManager())
    spec = openapi_mod.build(handler.routes.keys())
    text = json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        target = Path(args.out)
        # 用户会把文档里的 `--out doc/openapi.json` 原样复制执行；
        # 父目录不存在时不能只抛 FileNotFoundError（仓里其余写盘处都建了父目录）。
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
        print(f"已写出契约：{target}（{len(spec['paths'])} 条路径）")
        return 0
    print(text)
    return 0
