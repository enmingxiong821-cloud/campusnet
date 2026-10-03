"""本地 HTTP API 服务：账号管理、探活、手动登录、事件流、静态前端。"""

from __future__ import annotations

import json
import mimetypes
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from . import __version__, discovery, portal, probe as probe_mod
from .monitor import MonitorManager
from .paths import API_HOST, API_PORT, FRONTEND_DIR, stdout_ready
from .storage import Account, Store

Handler = Callable[[dict[str, Any], "ApiHandler"], Any]


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _account_from_payload(payload: dict[str, Any], existing: Account | None = None) -> Account:
    base = existing or Account()

    def take(key: str, default: Any = "") -> Any:
        return payload.get(key, default)

    account = Account(
        id=base.id,
        name=str(take("name", base.name)).strip(),
        portal_url=str(take("portal_url", base.portal_url)).strip(),
        probe_url=str(take("probe_url", base.probe_url) or "http://www.msftconnecttest.com/connecttest.txt").strip(),
        username=str(take("username", base.username)).strip(),
        password=str(take("password", base.password)),
        username_field=str(take("username_field", base.username_field)).strip(),
        password_field=str(take("password_field", base.password_field)).strip(),
        password_mode=str(take("password_mode", base.password_mode) or "plain"),
        login_mode=str(take("login_mode", base.login_mode) or "http"),
        submit_button=str(take("submit_button", base.submit_button)).strip(),
        max_retries=int(take("max_retries", base.max_retries) or 3),
        enabled=bool(take("enabled", base.enabled)),
    )
    extra = take("extra_fields", base.extra_fields) or {}
    account.extra_fields = {str(k): str(v) for k, v in extra.items()} if isinstance(extra, dict) else {}

    if not account.name:
        raise ApiError(HTTPStatus.BAD_REQUEST, "账号名称不能为空")
    if not account.portal_url:
        raise ApiError(HTTPStatus.BAD_REQUEST, "门户地址不能为空")
    parsed = urllib.parse.urlsplit(account.portal_url)
    if parsed.scheme not in ("http", "https"):
        raise ApiError(HTTPStatus.BAD_REQUEST, "门户地址必须以 http:// 或 https:// 开头")
    if account.password_mode not in ("plain", "base64", "md5", "md5_base64", "sha256"):
        raise ApiError(HTTPStatus.BAD_REQUEST, f"不支持的密码编码方式: {account.password_mode}")
    if account.login_mode not in ("http", "browser", "manual"):
        raise ApiError(HTTPStatus.BAD_REQUEST, f"不支持的登录模式: {account.login_mode}")
    if not account.password and existing is not None and not payload.get("password"):
        account.password = existing.password
    return account


class ApiHandler(BaseHTTPRequestHandler):
    server_version = f"campusnet/{__version__}"
    protocol_version = "HTTP/1.1"

    store: Store
    manager: MonitorManager
    routes: dict[tuple[str, str], Handler]

    # ---------- 基础工具 ----------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, D102
        return  # 静默，避免刷屏

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "请求体必须是 JSON 对象")
        return data

    def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
        if content_type.startswith("application/json"):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        else:
            body = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---------- 路由分发 ----------
    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        handler = self.routes.get((method, path))
        if handler is None:
            allowed = sorted({m for (m, p) in self.routes if p == path})
            if allowed:
                self._send(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"ok": False, "error": f"{path} 只支持 {'/'.join(allowed)}"},
                )
            else:
                self._serve_static(path)
            return
        try:
            handler({"query": query, "path": path}, self)
        except ApiError as exc:
            self._send(exc.status, {"ok": False, "error": exc.message})
        except Exception as exc:  # noqa: BLE001 - API 不能因单请求崩掉
            self.store.add_event(f"API 处理异常 {method} {path}: {exc}", level="error", stage="api")
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            )

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    # ---------- 静态资源 ----------
    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path == "/" else path.lstrip("/")
        target = (FRONTEND_DIR / rel).resolve()
        try:
            target.relative_to(FRONTEND_DIR.resolve())
        except ValueError:
            self._send(HTTPStatus.FORBIDDEN, {"ok": False, "error": "非法路径"})
            return
        if not target.is_file():
            self._send(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"找不到 {path}"})
            return
        ctype, _ = mimetypes.guess_type(str(target))
        self._send(HTTPStatus.OK, target.read_bytes(), ctype or "application/octet-stream")


# ---------------------------------------------------------------- 路由实现


def routes_accounts(store: Store) -> dict[tuple[str, str], Handler]:
    def list_accounts(_: dict[str, Any], h: ApiHandler) -> None:
        h._send(HTTPStatus.OK, {"ok": True, "accounts": [a.public() for a in store.list_accounts()]})

    def create_account(ctx: dict[str, Any], h: ApiHandler) -> None:
        payload = h._read_body()
        account = _account_from_payload(payload)
        new_id = store.add_account(account)
        store.add_event(f"新增账号 {account.name}", level="info", stage="account", account_id=new_id)
        h._send(HTTPStatus.CREATED, {"ok": True, "id": new_id, "account": account.public()})

    def _account_id(ctx: dict[str, Any]) -> int:
        params = ctx.get("params") or {}
        raw = params.get("id")
        if raw is None:
            raise ApiError(HTTPStatus.BAD_REQUEST, "缺少账号 ID")
        try:
            return int(raw)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"账号 ID 非法: {raw}") from exc

    def update_account(ctx: dict[str, Any], h: ApiHandler) -> None:
        account_id = _account_id(ctx)
        existing = store.get_account(account_id)
        if existing is None:
            raise ApiError(HTTPStatus.NOT_FOUND, f"账号 {account_id} 不存在")
        account = _account_from_payload(h._read_body(), existing)
        account.id = account_id
        store.update_account(account_id, account)
        store.add_event(f"更新账号 {account.name}", level="info", stage="account", account_id=account_id)
        h._send(HTTPStatus.OK, {"ok": True, "account": account.public()})

    def delete_account(ctx: dict[str, Any], h: ApiHandler) -> None:
        account_id = _account_id(ctx)
        if not store.delete_account(account_id):
            raise ApiError(HTTPStatus.NOT_FOUND, f"账号 {account_id} 不存在")
        store.add_event(f"删除账号 #{account_id}", level="warning", stage="account")
        h._send(HTTPStatus.OK, {"ok": True})

    return {
        ("GET", "/api/accounts"): list_accounts,
        ("POST", "/api/accounts"): create_account,
        ("PUT", "/api/accounts/{id}"): update_account,
        ("DELETE", "/api/accounts/{id}"): delete_account,
    }


def routes_actions(store: Store, manager: MonitorManager) -> dict[tuple[str, str], Handler]:
    def probe_now(ctx: dict[str, Any], h: ApiHandler) -> None:
        url = (ctx["query"].get("url") or [probe_mod.DEFAULT_PROBES[0]])[0]
        timeout = float((ctx["query"].get("timeout") or ["5"])[0])
        result = probe_mod.probe(url, timeout=timeout)
        h._send(HTTPStatus.OK, {"ok": True, "result": result.__dict__})

    def detect_portal(ctx: dict[str, Any], h: ApiHandler) -> None:
        url = (ctx["query"].get("url") or ["http://www.msftconnecttest.com/connecttest.txt"])[0]
        found = probe_mod.detect_portal_url(url)
        h._send(
            HTTPStatus.OK,
            {"ok": True, "portal_url": found, "hint": None if found else "未能从跳转中识别门户地址，请手动填写"},
        )

    def discover_portal(ctx: dict[str, Any], h: ApiHandler) -> None:
        query = ctx["query"]
        seed = (query.get("seed") or [""])[0]
        timeout = float((query.get("timeout") or ["4"])[0])
        scan = (query.get("scan") or ["1"])[0] not in ("0", "false", "no")
        report = discovery.discover(seed=seed, timeout=timeout, include_gateway_scan=scan)
        best = report["candidates"][0] if report["candidates"] else None
        h._send(
            HTTPStatus.OK,
            {
                "ok": True,
                "report": report,
                "best": best,
                "account_fields": discovery.to_account_fields(best) if best else None,
            },
        )

    def login_now(ctx: dict[str, Any], h: ApiHandler) -> None:
        params = ctx.get("params") or {}
        try:
            account_id = int(params.get("id", ""))
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"账号 ID 非法: {params.get('id')}") from exc
        account = store.get_account(account_id)
        if account is None:
            raise ApiError(HTTPStatus.NOT_FOUND, f"账号 {account_id} 不存在")
        monitor = manager.ensure(store)
        result = portal.login(account, headless=monitor.headless)
        store.add_event(
            f"手动登录 {account.name}: {result.state} - {result.message}",
            level="info" if result.ok else "warning",
            stage="manual_login",
            state=result.state,
            account_id=account_id,
        )
        store.set_state(account_id, "logged_in" if result.ok else "failed")
        h._send(
            HTTPStatus.OK,
            {"ok": True, "state": result.state, "message": result.message, "detail": result.detail},
        )

    def check_all(_: dict[str, Any], h: ApiHandler) -> None:
        monitor = manager.ensure(store)
        results = monitor.check_all()
        h._send(HTTPStatus.OK, {"ok": True, "results": results})

    return {
        ("GET", "/api/probe"): probe_now,
        ("GET", "/api/detect-portal"): detect_portal,
        ("GET", "/api/discover"): discover_portal,
        ("POST", "/api/accounts/{id}/login"): login_now,
        ("POST", "/api/check"): check_all,
    }


def routes_monitor(store: Store, manager: MonitorManager) -> dict[tuple[str, str], Handler]:
    def status(_: dict[str, Any], h: ApiHandler) -> None:
        h._send(HTTPStatus.OK, {"ok": True, "status": manager.ensure(store).status()})

    def start(ctx: dict[str, Any], h: ApiHandler) -> None:
        payload = h._read_body()
        monitor = manager.ensure(store)
        if "interval" in payload:
            from .monitor import Monitor

            monitor.interval = Monitor._clamp(int(payload["interval"]))
            store.set_setting("interval", monitor.interval)
        if "headless" in payload:
            monitor.headless = bool(payload["headless"])
            store.set_setting("headless", monitor.headless)
        if "autostart" in payload:
            store.set_setting("autostart", bool(payload["autostart"]))
        started = monitor.start()
        store.add_event(
            f"监控{'已启动' if started else '已在运行中'}，间隔 {monitor.interval} 秒",
            level="info",
            stage="monitor",
        )
        h._send(HTTPStatus.OK, {"ok": True, "started": started, "status": monitor.status()})

    def stop(_: dict[str, Any], h: ApiHandler) -> None:
        stopped = manager.monitor.stop() if manager.monitor else True
        store.add_event("监控已停止", level="info", stage="monitor")
        h._send(HTTPStatus.OK, {"ok": True, "stopped": stopped})

    return {
        ("GET", "/api/monitor"): status,
        ("POST", "/api/monitor/start"): start,
        ("POST", "/api/monitor/stop"): stop,
    }


def routes_events(store: Store) -> dict[tuple[str, str], Handler]:
    def list_events(ctx: dict[str, Any], h: ApiHandler) -> None:
        query = ctx["query"]
        limit = int((query.get("limit") or ["200"])[0])
        account_raw = (query.get("account_id") or [""])[0]
        account_id = int(account_raw) if account_raw else None
        h._send(
            HTTPStatus.OK,
            {"ok": True, "events": store.list_events(limit=limit, account_id=account_id)},
        )

    def clear_events(_: dict[str, Any], h: ApiHandler) -> None:
        store.clear_events()
        h._send(HTTPStatus.OK, {"ok": True})

    return {
        ("GET", "/api/events"): list_events,
        ("POST", "/api/events/clear"): clear_events,
    }


def routes_system(store: Store) -> dict[tuple[str, str], Handler]:
    def health(_: dict[str, Any], h: ApiHandler) -> None:
        h._send(HTTPStatus.OK, {"ok": True, "version": __version__, "accounts": len(store.list_accounts())})

    def healthz(_: dict[str, Any], h: ApiHandler) -> None:
        h._send(HTTPStatus.OK, {"ok": True, "version": __version__})

    def settings_update(ctx: dict[str, Any], h: ApiHandler) -> None:
        payload = h._read_body()
        for key, value in payload.items():
            store.set_setting(str(key), value)
        h._send(HTTPStatus.OK, {"ok": True, "settings": store.all_settings()})

    def settings_get(_: dict[str, Any], h: ApiHandler) -> None:
        h._send(HTTPStatus.OK, {"ok": True, "settings": store.all_settings()})

    return {
        ("GET", "/api/health"): health,
        ("GET", "/healthz"): healthz,
        ("GET", "/api/settings"): settings_get,
        ("PUT", "/api/settings"): settings_update,
    }


def build_handler(store: Store, manager: MonitorManager) -> type[ApiHandler]:
    routes: dict[tuple[str, str], Handler] = {}
    for group in (
        routes_accounts(store),
        routes_actions(store, manager),
        routes_monitor(store, manager),
        routes_events(store),
        routes_system(store),
    ):
        routes.update(group)

    handler_cls = type(
        "BoundApiHandler",
        (ApiHandler,),
        {"store": store, "manager": manager, "routes": routes},
    )
    return handler_cls


def _match_route(handler: type[ApiHandler], method: str, path: str) -> tuple[Handler | None, dict[str, str]]:
    """把 /api/accounts/3 映射到模板 /api/accounts/{id}。"""
    exact = handler.routes.get((method, path))
    if exact is not None:
        return exact, {}
    parts = path.strip("/").split("/")
    for (route_method, template), fn in handler.routes.items():
        if route_method != method:
            continue
        tparts = template.strip("/").split("/")
        if len(tparts) != len(parts):
            continue
        params: dict[str, str] = {}
        ok = True
        for t, p in zip(tparts, parts):
            if t.startswith("{") and t.endswith("}"):
                params[t[1:-1]] = p
            elif t != p:
                ok = False
                break
        if ok:
            return fn, params
    return None, {}


def _install_dispatch(handler: type[ApiHandler]) -> None:
    def _dispatch(self: ApiHandler, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        fn, params = _match_route(handler, method, path)
        if fn is None:
            allowed = sorted({m for (m, p) in handler.routes if p == path})
            if allowed:
                self._send(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"ok": False, "error": f"{path} 只支持 {'/'.join(allowed)}"},
                )
            else:
                self._serve_static(path)
            return
        try:
            fn({"query": query, "path": path, "params": params}, self)
        except ApiError as exc:
            self._send(exc.status, {"ok": False, "error": exc.message})
        except Exception as exc:  # noqa: BLE001
            self.store.add_event(f"API 处理异常 {method} {path}: {exc}", level="error", stage="api")
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            )

    handler.do_GET = lambda self: _dispatch(self, "GET")  # type: ignore[method-assign]
    handler.do_POST = lambda self: _dispatch(self, "POST")  # type: ignore[method-assign]
    handler.do_PUT = lambda self: _dispatch(self, "PUT")  # type: ignore[method-assign]
    handler.do_DELETE = lambda self: _dispatch(self, "DELETE")  # type: ignore[method-assign]


def serve(
    store: Store | None = None,
    host: str = API_HOST,
    port: int = API_PORT,
    autostart: bool = True,
    blocking: bool = True,
) -> ThreadingHTTPServer:
    """启动 API + 前端静态服务。"""
    stdout_ready()
    store = store or Store()
    manager = MonitorManager()
    handler_cls = build_handler(store, manager)
    _install_dispatch(handler_cls)

    httpd = ThreadingHTTPServer((host, port), handler_cls)
    httpd.daemon_threads = True

    if autostart:
        want = bool(store.get_setting("autostart", True))
        if want:
            manager.start(store)
        store.add_event(
            f"服务启动 http://{host}:{port}（自动监控={'开' if want else '关'}）",
            level="info",
            stage="server",
        )

    if not blocking:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd

    print(f"校园网自连服务已启动： http://{host}:{port}  （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止监控与服务…")
    finally:
        manager.stop()
        httpd.server_close()
    return httpd