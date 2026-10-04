"""本地模拟校园网门户 + 探测服务器，用于自测完整掉线重连链路。

本机网络处于已放行状态，直接测只会得到「在线」，什么也验证不了。
这个脚本把整套链路在一台机器上复现出来：

    探活地址被开关关掉 → 判定掉线 → 解析登录表单 → 提交 → 复检放行 → 判定恢复

模拟的是 Dr.COM / 深澜形态的门户：字段名 DDDDD / upass，密码 Base64，
隐藏域 0MKKey / user_ip / user_mac，页面用 GB2312 编码（顺带验证 charset 修复）。

用法：
    python utils/mock_portal.py                    # 默认门户 2270 / 探测 2271
    python utils/mock_portal.py --portal-port 2270 --probe-port 2271
    python utils/mock_portal.py --drop             # 直接以掉线状态启动

配套自测流程见 doc/自测指南.md。
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "project" / "backend"))

from campusnet import ports  # noqa: E402

DEFAULT_PORTAL_PORT = 2270
DEFAULT_PROBE_PORT = 2271

USERNAME = "20240001"
PASSWORD = "campus@2024"

# 掉线/放行开关，由 /admin 页面或 POST /admin/toggle 修改
STATE = {"online": True}
PROBE_PORT = DEFAULT_PROBE_PORT

# 页面内容放在运行时字典里：serve() 要替换占位符，
# 但绝不能像之前那样去覆盖 Handler 的方法 —— 那会把登录接口一起堵死。
PAGES: dict[str, str] = {}


class _Base(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # 只打印关键请求，避免刷屏
        path = str(args[0]) if args else ""
        if "/admin" not in path and path not in ("GET /probe",):
            return
        print(f"  [mock] {self.command} {path}")

    def _send(
        self,
        status: int,
        body: bytes | str,
        content_type: str = "text/html; charset=gb2312",
        extra: dict[str, str] | None = None,
    ) -> None:
        if isinstance(body, str):
            body = body.encode("gb2312", errors="replace")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        # 必须传 bytes：直接传 str 会被 _send 按 gb2312 编码，
        # 与这里声明的 utf-8 冲突，中文消息会变成乱码。
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            content_type="application/json; charset=utf-8",
        )

    def _redirect(self, location: str, status: int = HTTPStatus.FOUND) -> None:
        self._send(status, b"", extra={"Location": location})


class PortalHandler(_Base):
    """模拟认证门户。"""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path

        if path == "/":
            self._redirect("/eportal/portal/login")
            return

        if path == "/eportal/portal/login":
            self._send(HTTPStatus.OK, PAGES["login"])
            return

        if path == "/admin":
            self._send(HTTPStatus.OK, PAGES["admin"])
            return

        if path == "/admin/state":
            self._json(HTTPStatus.OK, {"online": STATE["online"], "probe_port": PROBE_PORT})
            return

        self._send(HTTPStatus.NOT_FOUND, f"<h1>404 {html.escape(path)}</h1>")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path

        if path == "/admin/toggle":
            params = parse_qs(parsed.query)
            if "online" in params:
                STATE["online"] = params["online"][0] not in ("0", "false")
            else:
                STATE["online"] = not STATE["online"]
            print(f"  [mock] 状态切到 {'放行' if STATE['online'] else '掉线'}")
            self._json(HTTPStatus.OK, {"ok": True, "online": STATE["online"]})
            return

        if path == "/eportal/portal/login":
            self._handle_login()
            return

        self._send(HTTPStatus.NOT_FOUND, "<h1>404</h1>")

    def _handle_login(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        print(f"  [mock] 收到登录提交，字段：{sorted(form)}")

        # 模拟门户对隐藏域的强校验：缺一个就拒
        for required in ("0MKKey", "user_ip", "user_mac"):
            if required not in form:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"result": 0, "message": f"missing hidden field {required}"},
                )
                return

        # 模拟 Base64 密码校验
        import base64

        expected = base64.b64encode(PASSWORD.encode()).decode()
        if form.get("DDDDD") != USERNAME or form.get("upass") != expected:
            self._json(HTTPStatus.OK, {"result": 0, "message": "密码错误"})
            return

        STATE["online"] = True
        print("  [mock] 认证通过，网络已放行")
        self._json(HTTPStatus.OK, {"result": 1, "message": "success"})


class ProbeHandler(_Base):
    """模拟明文探测地址。掉线时 302 跳门户，这是校园网最典型的行为。"""

    def do_GET(self) -> None:  # noqa: N802
        if STATE["online"]:
            self._send(
                HTTPStatus.OK,
                "Microsoft Connect Test",
                content_type="text/plain; charset=us-ascii",
            )
        else:
            print("  [mock] 探测被重定向到门户（判定未放行）")
            self._redirect("http://127.0.0.1:%d/eportal/portal/login" % PORTAL_PORT)


LOGIN_PAGE = """<html><head>
<meta http-equiv="Content-Type" content="text/html; charset=gb2312">
<title>Dr.COM 认证系统 - 深澜网络</title>
</head><body>
<h2>校园网用户认证</h2>
<form action="/eportal/portal/login" method="post">
  <input type="hidden" name="user_ip" value="10.110.168.53">
  <input type="hidden" name="user_mac" value="AA-BB-CC-DD-EE-FF">
  <input type="hidden" name="0MKKey" value="">
  <p>用户名称 <input type="text" name="DDDDD" value=""></p>
  <p>用户密码 <input type="password" name="upass" value=""></p>
  <p><input type="submit" name="submitBtn" value="登录"></p>
</form>
</body></html>
"""

ADMIN_PAGE = """<html><head><meta charset="utf-8"><title>Mock 控制台</title></head><body>
<h2>本地模拟门户</h2>
<p>账号 <code>__USER__</code> / 密码 <code>__PASS__</code>（提交时按 Base64）</p>
<p>当前状态：<strong id="s">-</strong></p>
<p><button onclick="t()">切换 放行 / 掉线</button></p>
<script>
async function t(){ await fetch('/admin/toggle',{method:'POST'}); s(); }
async function s(){
  const r = await (await fetch('/admin/state')).json();
  document.getElementById('s').textContent = r.online ? '放行' : '掉线';
}
s();
</script>
</body></html>
"""


def serve(
    portal_port: int = DEFAULT_PORTAL_PORT,
    probe_port: int = DEFAULT_PROBE_PORT,
    drop: bool = False,
    auto_shift: bool = False,
) -> None:
    global PORTAL_PORT
    STATE["online"] = not drop

    PAGES["login"] = LOGIN_PAGE
    PAGES["admin"] = ADMIN_PAGE.replace("__USER__", USERNAME).replace("__PASS__", PASSWORD)

    # 先探端口：直接构造 HTTPServer 时端口被占会抛一个
    # 新手读不懂的 traceback，而现象是命令永远不返回。
    try:
        resolved = ports.resolve_ports(
            {portal_port: "门户", probe_port: "探测"},
            auto_shift=auto_shift,
        )
    except OSError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        print(
            "        端口被占时新开一个终端跑：\n"
            f"          python {Path(__file__).name} --portal-port 2290 --probe-port 2291\n"
            "        或先关掉占用它的那个程序。",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    portal_port = resolved[portal_port]
    probe_port = resolved[probe_port]
    PORTAL_PORT = portal_port

    portal = ThreadingHTTPServer(("127.0.0.1", portal_port), PortalHandler)
    probe = ThreadingHTTPServer(("127.0.0.1", probe_port), ProbeHandler)
    for server in (portal, probe):
        server.daemon_threads = True

    threading.Thread(target=probe.serve_forever, daemon=True).start()

    print("=" * 68)
    print(" 本地模拟校园网已启动")
    print(f"   门户页面  http://127.0.0.1:{portal_port}/eportal/portal/login")
    print(f"   探测地址  http://127.0.0.1:{probe_port}/probe")
    print(f"   控制台    http://127.0.0.1:{portal_port}/admin   ← 点这个切换掉线/放行")
    print(f"   初始状态  {'放行' if STATE['online'] else '掉线'}")
    print(f"   账号/密码  {USERNAME} / {PASSWORD}")
    print("=" * 68)
    print("  Ctrl+C 停止\n")

    try:
        portal.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        portal.shutdown()
        probe.shutdown()
        portal.server_close()
        probe.server_close()


def _bind_page(handler: Any, _name: str, page: str) -> Any:
    """已废弃：曾用它把页面挂到 Handler 上，结果连 POST 登录接口一起覆盖掉了。"""
    raise NotImplementedError("改用 PAGES 字典，不要覆盖 Handler 方法")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mock_portal", description="本地模拟校园网门户，用于自测掉线重连链路"
    )
    parser.add_argument("--portal-port", type=int, default=DEFAULT_PORTAL_PORT)
    parser.add_argument("--probe-port", type=int, default=DEFAULT_PROBE_PORT)
    parser.add_argument("--drop", action="store_true", help="以掉线状态启动")
    parser.add_argument(
        "--auto-shift",
        action="store_true",
        help="端口被占时自动往后找一个空闲端口",
    )
    args = parser.parse_args(argv)

    serve(args.portal_port, args.probe_port, args.drop, auto_shift=args.auto_shift)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())