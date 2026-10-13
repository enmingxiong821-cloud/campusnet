"""本地 HTTP API 服务：账号管理、探活、手动登录、事件流、静态前端。"""

from __future__ import annotations

import json
import mimetypes
import sys
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from . import __version__, bootstart, discovery, portal, ports, probe as probe_mod
from .auth import ApiAuth
from .lockfile import SingleInstance
from .monitor import MonitorManager
from .paths import API_HOST, API_PORT, FRONTEND_DIR, instance_lock_path, stdout_ready
from .storage import Account, Store

Handler = Callable[[dict[str, Any], "ApiHandler"], Any]
MAX_REQUEST_BODY = 1024 * 1024
BODY_READ_TIMEOUT = 5.0


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _account_from_payload(
    payload: dict[str, Any], existing: Account | None = None
) -> Account:
    base = existing or Account()

    def take(key: str, default: Any = "") -> Any:
        return payload.get(key, default)

    account = Account(
        id=base.id,
        name=str(take("name", base.name)).strip(),
        portal_url=str(take("portal_url", base.portal_url)).strip(),
        probe_url=str(
            take("probe_url", base.probe_url)
            or "http://www.msftconnecttest.com/connecttest.txt"
        ).strip(),
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
    account.extra_fields = (
        {str(k): str(v) for k, v in extra.items()} if isinstance(extra, dict) else {}
    )

    if not account.name:
        raise ApiError(HTTPStatus.BAD_REQUEST, "账号名称不能为空")
    if not account.portal_url:
        raise ApiError(HTTPStatus.BAD_REQUEST, "门户地址不能为空")
    parsed = urllib.parse.urlsplit(account.portal_url)
    if parsed.scheme not in ("http", "https"):
        raise ApiError(
            HTTPStatus.BAD_REQUEST, "门户地址必须以 http:// 或 https:// 开头"
        )
    if account.password_mode not in ("plain", "base64", "md5", "md5_base64", "sha256"):
        raise ApiError(
            HTTPStatus.BAD_REQUEST, f"不支持的密码编码方式: {account.password_mode}"
        )
    if account.login_mode not in ("http", "browser", "manual", "srun"):
        raise ApiError(
            HTTPStatus.BAD_REQUEST, f"不支持的登录模式: {account.login_mode}"
        )
    if not account.password and existing is not None and not payload.get("password"):
        account.password = existing.password
    return account


class ApiHandler(BaseHTTPRequestHandler):
    server_version = f"campusnet/{__version__}"
    protocol_version = "HTTP/1.1"

    store: Store
    manager: MonitorManager
    routes: dict[tuple[str, str], Handler]
    auth: ApiAuth | None
    auth_required: bool

    # ---------- 基础工具 ----------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, D102
        return  # 静默，避免刷屏

    def _read_raw_body(self) -> bytes:
        """有界读取并缓存本次请求体，包括随后被拒绝的请求。"""
        if hasattr(self, "_request_body"):
            return self._request_body
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise ApiError(HTTPStatus.BAD_REQUEST, "不支持 Transfer-Encoding")
        lengths = self.headers.get_all("Content-Length", [])
        try:
            if len(lengths) > 1:
                raise ValueError("重复长度")
            length = int(lengths[0]) if lengths else 0
            if length < 0:
                raise ValueError("负长度")
        except ValueError as exc:
            self.close_connection = True
            raise ApiError(HTTPStatus.BAD_REQUEST, "Content-Length 无效") from exc
        if length > MAX_REQUEST_BODY:
            self.close_connection = True
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求体超过 1 MiB 上限")
        previous_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(BODY_READ_TIMEOUT)
            raw = self.rfile.read(length) if length else b""
        except (TimeoutError, OSError) as exc:
            self.close_connection = True
            raise ApiError(
                HTTPStatus.REQUEST_TIMEOUT, "请求体读取超时或连接中断"
            ) from exc
        finally:
            self.connection.settimeout(previous_timeout)
        if len(raw) != length:
            self.close_connection = True
            raise ApiError(HTTPStatus.BAD_REQUEST, "请求体长度不足")
        self._request_body = raw
        return raw

    def _read_body(self) -> dict[str, Any]:
        raw = self._read_raw_body()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, f"请求体不是合法 JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "请求体必须是 JSON 对象")
        return data

    def _send(
        self,
        status: int,
        payload: Any,
        content_type: str = "application/json; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        if content_type.startswith("application/json"):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        else:
            body = (
                payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
            )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
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
            self.store.add_event(
                f"API 处理异常 {method} {path}: {exc}", level="error", stage="api"
            )
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
        self._send(
            HTTPStatus.OK, target.read_bytes(), ctype or "application/octet-stream"
        )


# ---------------------------------------------------------------- 路由实现


def routes_accounts(store: Store) -> dict[tuple[str, str], Handler]:
    def list_accounts(_: dict[str, Any], h: ApiHandler) -> None:
        h._send(
            HTTPStatus.OK,
            {"ok": True, "accounts": [a.public() for a in store.list_accounts()]},
        )

    def create_account(ctx: dict[str, Any], h: ApiHandler) -> None:
        payload = h._read_body()
        account = _account_from_payload(payload)
        new_id = store.add_account(account)
        store.add_event(
            f"新增账号 {account.name}", level="info", stage="account", account_id=new_id
        )
        h._send(
            HTTPStatus.CREATED, {"ok": True, "id": new_id, "account": account.public()}
        )

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
        store.add_event(
            f"更新账号 {account.name}",
            level="info",
            stage="account",
            account_id=account_id,
        )
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


def routes_actions(
    store: Store, manager: MonitorManager
) -> dict[tuple[str, str], Handler]:
    def probe_now(ctx: dict[str, Any], h: ApiHandler) -> None:
        url = (ctx["query"].get("url") or [probe_mod.DEFAULT_PROBES[0]])[0]
        timeout = float((ctx["query"].get("timeout") or ["5"])[0])
        result = probe_mod.probe(url, timeout=timeout)
        h._send(HTTPStatus.OK, {"ok": True, "result": result.__dict__})

    def detect_portal(ctx: dict[str, Any], h: ApiHandler) -> None:
        url = (
            ctx["query"].get("url")
            or ["http://www.msftconnecttest.com/connecttest.txt"]
        )[0]
        found = probe_mod.detect_portal_url(url)
        h._send(
            HTTPStatus.OK,
            {
                "ok": True,
                "portal_url": found,
                "hint": None if found else "未能从跳转中识别门户地址，请手动填写",
            },
        )

    def discover_portal(ctx: dict[str, Any], h: ApiHandler) -> None:
        query = ctx["query"]
        seed = (query.get("seed") or [""])[0]
        timeout = float((query.get("timeout") or ["4"])[0])
        scan = (query.get("scan") or ["1"])[0] not in ("0", "false", "no")
        report = discovery.discover(
            seed=seed, timeout=timeout, include_gateway_scan=scan
        )
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
            raise ApiError(
                HTTPStatus.BAD_REQUEST, f"账号 ID 非法: {params.get('id')}"
            ) from exc
        account = store.get_account(account_id)
        if account is None:
            raise ApiError(HTTPStatus.NOT_FOUND, f"账号 {account_id} 不存在")
        monitor = manager.ensure(store)
        # 手动登录也吐进度：浏览器模式下可能要等十几秒，不能让前端干等
        result = portal.login(
            account,
            headless=monitor.headless,
            on_progress=lambda stage, message: monitor.emit_progress(
                account, stage, message
            ),
        )
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
            {
                "ok": True,
                "state": result.state,
                "message": result.message,
                "detail": result.detail,
            },
        )

    def progress(ctx: dict[str, Any], h: ApiHandler) -> None:
        """增量拉取实时进度。前端带上次 seq 回来，只拿新增部分。"""
        query = ctx["query"]
        try:
            seq = int((query.get("seq") or ["0"])[0])
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "seq 必须是整数") from None
        account_raw = (query.get("account_id") or [""])[0]
        try:
            account_id = int(account_raw) if account_raw else None
        except ValueError:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, f"account_id 非法: {account_raw}"
            ) from None

        monitor = manager.ensure(store)
        if (query.get("latest") or [""])[0] not in ("", "0", "false"):
            payload = monitor.latest_progress(
                limit=int((query.get("limit") or ["30"])[0])
            )
        else:
            payload = monitor.progress_since(seq, account_id=account_id)
        payload["ok"] = True
        payload["checking"] = monitor.running
        h._send(HTTPStatus.OK, payload)

    def check_all(_: dict[str, Any], h: ApiHandler) -> None:
        monitor = manager.ensure(store)
        results = monitor.check_all()
        h._send(HTTPStatus.OK, {"ok": True, "results": results})

    return {
        ("GET", "/api/probe"): probe_now,
        ("GET", "/api/detect-portal"): detect_portal,
        ("GET", "/api/discover"): discover_portal,
        ("GET", "/api/progress"): progress,
        ("POST", "/api/accounts/{id}/login"): login_now,
        ("POST", "/api/check"): check_all,
    }


def routes_monitor(
    store: Store, manager: MonitorManager
) -> dict[tuple[str, str], Handler]:
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
        h._send(
            HTTPStatus.OK, {"ok": True, "started": started, "status": monitor.status()}
        )

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
            {
                "ok": True,
                "events": store.list_events(limit=limit, account_id=account_id),
            },
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
        h._send(
            HTTPStatus.OK,
            {
                "ok": True,
                "version": __version__,
                "accounts": len(store.list_accounts()),
            },
        )

    def healthz(_: dict[str, Any], h: ApiHandler) -> None:
        h._send(HTTPStatus.OK, {"ok": True, "version": __version__})

    def settings_update(ctx: dict[str, Any], h: ApiHandler) -> None:
        payload = h._read_body()
        for key, value in payload.items():
            store.set_setting(str(key), value)
        h._send(HTTPStatus.OK, {"ok": True, "settings": store.all_settings()})

    def settings_get(_: dict[str, Any], h: ApiHandler) -> None:
        h._send(HTTPStatus.OK, {"ok": True, "settings": store.all_settings()})

    def bootstart_status(_: dict[str, Any], h: ApiHandler) -> None:
        h._send(HTTPStatus.OK, {"ok": True, **bootstart.status().as_dict()})

    def validate_bootstart_request(h: ApiHandler) -> None:
        # Rejected requests may have unread bodies; do not reuse that connection.
        if (
            h.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            h.close_connection = True
            raise ApiError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "自启动操作要求 application/json"
            )

    def bootstart_install(ctx: dict[str, Any], h: ApiHandler) -> None:
        validate_bootstart_request(h)
        payload = h._read_body()
        address = h.server.server_address
        if not isinstance(address, tuple):
            raise ApiError(
                HTTPStatus.INTERNAL_SERVER_ERROR, "服务监听地址不是 TCP 地址"
            )
        host = payload.get("host", address[0])
        raw_port = payload.get("port", address[1])
        try:
            if (
                not isinstance(host, str)
                or isinstance(raw_port, bool)
                or not isinstance(raw_port, (int, str))
            ):
                raise ValueError("host 必须为字符串，port 必须为整数")
            port = int(raw_port)
            bootstart.launch_args(host, port)
        except (ValueError, TypeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"启动参数无效：{exc}") from exc
        result = bootstart.install(host=host, port=port)
        store.add_event(
            f"开机自启动安装请求 -> {'成功' if result.installed else '未生效'}：{result.detail}",
            level="info" if result.installed else "warning",
            stage="server",
        )
        h._send(
            HTTPStatus.OK if result.success else HTTPStatus.INTERNAL_SERVER_ERROR,
            {
                "ok": result.success,
                "error": "" if result.success else result.detail,
                **result.as_dict(),
            },
        )

    def bootstart_uninstall(_: dict[str, Any], h: ApiHandler) -> None:
        validate_bootstart_request(h)
        h._read_body()
        result = bootstart.uninstall()
        store.add_event(
            f"开机自启动卸载请求：{result.detail}",
            level="info" if result.success else "warning",
            stage="server",
        )
        h._send(
            HTTPStatus.OK if result.success else HTTPStatus.INTERNAL_SERVER_ERROR,
            {
                "ok": result.success,
                "error": "" if result.success else result.detail,
                **result.as_dict(),
            },
        )

    def power_info(_: dict[str, Any], h: ApiHandler) -> None:
        """远程开机需要的信息：网卡 MAC、唤醒命令与硬件路线说明（只读）。"""
        from . import power as power_mod

        h._send(HTTPStatus.OK, {"ok": True, **power_mod.wake_info()})

    def power_action(ctx: dict[str, Any], h: ApiHandler) -> None:
        """远程关机 / 重启：手机控制台上的一键操作，必须显式确认。"""
        from . import power as power_mod

        action = str((ctx.get("params") or {}).get("action", "")).lower()
        if action not in ("off", "reboot"):
            raise ApiError(HTTPStatus.NOT_FOUND, f"不支持的电源操作：{action}")
        payload = h._read_body()
        if not payload.get("confirm"):
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "关机/重启需要显式确认（confirm=true）"
            )
        try:
            command = power_mod.shutdown() if action == "off" else power_mod.reboot()
        except power_mod.PowerError as exc:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, f"执行失败：{exc}") from exc
        label = "关机" if action == "off" else "重启"
        store.add_event(
            f"收到远程{label}指令：{command}", level="warning", stage="power"
        )
        h._send(HTTPStatus.OK, {"ok": True, "action": action, "command": command})

    def doctor_power(_: dict[str, Any], h: ApiHandler) -> None:
        """电源体检（只读）：远程开关机前置条件逐项结论。"""
        from . import powerprep as powerprep_mod

        items = powerprep_mod.run(fix=False)
        h._send(HTTPStatus.OK, {"ok": True, "items": [i.as_dict() for i in items]})

    def doctor_power_fix(_: dict[str, Any], h: ApiHandler) -> None:
        """批量修复可自动化的项；与电源动作一样要求 confirm，走同源 + 会话保护。"""
        from . import powerprep as powerprep_mod

        payload = h._read_body()
        if not payload.get("confirm"):
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "修复系统设置需要显式确认（confirm=true）"
            )
        items = powerprep_mod.run(fix=True)
        store.add_event("电源体检执行了自动修复", level="warning", stage="power")
        h._send(HTTPStatus.OK, {"ok": True, "items": [i.as_dict() for i in items]})

    return {
        ("GET", "/api/health"): health,
        ("GET", "/healthz"): healthz,
        ("GET", "/api/settings"): settings_get,
        ("PUT", "/api/settings"): settings_update,
        ("GET", "/api/bootstart"): bootstart_status,
        ("POST", "/api/bootstart/install"): bootstart_install,
        ("POST", "/api/bootstart/uninstall"): bootstart_uninstall,
        ("POST", "/api/power/{action}"): power_action,
        ("GET", "/api/power/info"): power_info,
        ("GET", "/api/doctor/power"): doctor_power,
        ("POST", "/api/doctor/power/fix"): doctor_power_fix,
    }


def routes_auth(auth: ApiAuth | None) -> dict[tuple[str, str], Handler]:
    """认证入口公开，业务 API 由分发层统一保护。"""

    def status(_: dict[str, Any], h: ApiHandler) -> None:
        required = bool(getattr(h, "auth_required", False))
        authenticated = (
            bool(auth and auth.authenticated(h.headers)) if required else True
        )
        h._send(
            HTTPStatus.OK,
            {"ok": True, "required": required, "authenticated": authenticated},
        )

    def login(_: dict[str, Any], h: ApiHandler) -> None:
        if auth is None:
            h._send(
                HTTPStatus.OK, {"ok": True, "required": False, "authenticated": True}
            )
            return
        payload = h._read_body()
        token = str(payload.get("token", ""))
        session = auth.login(token)
        if session is None:
            h._send(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "error": "控制台令牌不正确"},
                extra_headers={"WWW-Authenticate": "Bearer"},
            )
            return
        h._send(
            HTTPStatus.OK,
            {"ok": True, "required": True, "authenticated": True},
            extra_headers={"Set-Cookie": auth.set_cookie(session)},
        )

    def logout(_: dict[str, Any], h: ApiHandler) -> None:
        if auth is not None:
            auth.logout(h.headers)
        h._send(
            HTTPStatus.OK,
            {"ok": True},
            extra_headers={"Set-Cookie": ApiAuth.clear_cookie()},
        )

    return {
        ("GET", "/api/auth/status"): status,
        ("POST", "/api/auth/login"): login,
        ("POST", "/api/auth/logout"): logout,
    }


def build_handler(
    store: Store,
    manager: MonitorManager,
    auth: ApiAuth | None = None,
    auth_required: bool = False,
) -> type[ApiHandler]:
    routes: dict[tuple[str, str], Handler] = {}
    for group in (
        routes_accounts(store),
        routes_actions(store, manager),
        routes_monitor(store, manager),
        routes_events(store),
        routes_system(store),
        routes_auth(auth),
    ):
        routes.update(group)

    handler_cls = type(
        "BoundApiHandler",
        (ApiHandler,),
        {
            "store": store,
            "manager": manager,
            "routes": routes,
            "auth": auth,
            "auth_required": auth_required,
        },
    )
    return handler_cls


def _match_route(
    handler: type[ApiHandler], method: str, path: str
) -> tuple[Handler | None, dict[str, str]]:
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


EXTENSION_SCHEME = "chrome-extension://"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _host_is_loopback(host_header: str) -> bool:
    """Host 头（可能带端口）是否指向本机回环。"""
    host = (host_header or "").strip().lower()
    if host.startswith("["):  # [::1]:2268
        return host[1:].split("]", 1)[0] in LOOPBACK_HOSTS
    return host.rsplit(":", 1)[0] in LOOPBACK_HOSTS


def _is_extension_origin(origin: str) -> bool:
    """是否浏览器扩展的来源（Origin 由浏览器设定，页面 JS 改不了）。"""
    if not origin.startswith(EXTENSION_SCHEME):
        return False
    rest = origin[len(EXTENSION_SCHEME) :]
    return bool(rest) and "/" not in rest


def require_same_origin(h: ApiHandler, *, token_ok: bool = False) -> None:
    """校验浏览器写请求；允许无 Origin 的本地 CLI 客户端。

    两条豁免，前提都是「请求方确实拿到了凭据」，网页拿不到：

    1. **显式令牌有效**（token_ok）。CSRF 的成因是浏览器自动附带 Cookie，而
       Authorization / X-CampusNet-Token 这类自定义头跨站页读不到、要发也会先触发
       预检——OWASP 的 CSRF 备忘单把「自定义请求头」列为有效防御。传入的必须是
       「令牌有效」而不是「带了头」，否则等于放行任何人。
    2. **浏览器扩展访问本机回环**。MV3 扩展 service worker 发非 GET 请求时，Origin
       必然是 `chrome-extension://<id>`（浏览器设定，见 w3c/webextensions#777），
       永远不可能等于 Host，于是被一刀切拦掉——这正是扩展「优先把检测交给本地服务」
       从来没生效过、一直静默回落的原因。网页的 Origin 只会是 http(s) 或 null，
       伪造不出这个 scheme，所以按 scheme 放行不会给网页写权限；再限定 Host 是回环，
       跨机访问仍然必须带令牌。
    """
    if token_ok:
        return
    origin = h.headers.get("Origin")
    if origin is not None and _is_extension_origin(origin):
        if _host_is_loopback(h.headers.get("Host", "")):
            return
    valid = True
    if origin is not None:
        try:
            parsed = urllib.parse.urlsplit(origin)
            valid = (
                parsed.scheme in ("http", "https")
                and bool(parsed.netloc)
                and parsed.netloc.lower() == h.headers.get("Host", "").lower()
                and parsed.username is None
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
    if not valid or h.headers.get("Sec-Fetch-Site") == "cross-site":
        h.close_connection = True
        raise ApiError(HTTPStatus.FORBIDDEN, "写操作不接受跨来源请求")


def _install_dispatch(handler: type[ApiHandler]) -> None:
    public_api = {
        ("GET", "/healthz"),
        ("GET", "/api/health"),
        ("GET", "/api/auth/status"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/logout"),
    }

    def _dispatch(self: ApiHandler, method: str) -> None:
        # 每个请求重置缓存，不能把 keep-alive 上一请求的正文重复使用。
        self.__dict__.pop("_request_body", None)
        try:
            self._read_raw_body()
        except ApiError as exc:
            self._send(exc.status, {"ok": False, "error": exc.message})
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path.startswith("/api/v1/"):
            # 版本化别名：/api/v1/accounts → /api/accounts。
            # 老前端与扩展继续用不带版本的路径，互不影响。
            path = "/api/" + path[len("/api/v1/") :]
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
        if (
            getattr(handler, "auth_required", False)
            and path.startswith("/api/")
            and (method, path) not in public_api
            and (self.auth is None or not self.auth.authenticated(self.headers))
        ):
            self._send(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "error": "需要先登录控制台"},
                extra_headers={"WWW-Authenticate": "Bearer"},
            )
            return
        try:
            if method in ("POST", "PUT", "DELETE", "PATCH"):
                require_same_origin(
                    self,
                    token_ok=bool(
                        self.auth and self.auth.explicit_token_ok(self.headers)
                    ),
                )
            fn({"query": query, "path": path, "params": params}, self)
        except ApiError as exc:
            self._send(exc.status, {"ok": False, "error": exc.message})
        except Exception as exc:  # noqa: BLE001
            self.store.add_event(
                f"API 处理异常 {method} {path}: {exc}", level="error", stage="api"
            )
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            )

    handler.do_GET = lambda self: _dispatch(self, "GET")  # type: ignore[method-assign]
    handler.do_POST = lambda self: _dispatch(self, "POST")  # type: ignore[method-assign]
    handler.do_PUT = lambda self: _dispatch(self, "PUT")  # type: ignore[method-assign]
    handler.do_DELETE = lambda self: _dispatch(self, "DELETE")  # type: ignore[method-assign]

    def _options(self: ApiHandler) -> None:
        """处理浏览器或代理发出的预检，避免 BaseHTTPRequestHandler 返回 501。"""
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Allow", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"
        )
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-CampusNet-Token",
        )
        self.end_headers()

    handler.do_OPTIONS = _options  # type: ignore[method-assign]


def serve(
    store: Store | None = None,
    host: str = API_HOST,
    port: int = API_PORT,
    autostart: bool = True,
    blocking: bool = True,
    auto_shift: bool = False,
    auth_required: bool | None = None,
) -> ThreadingHTTPServer:
    """启动 API + 前端静态服务。

    端口被占时默认直接报错退出，而不是抛一串用户读不懂的
    OSError traceback —— 新手看到「端口被占，换一个」才知道该做什么。
    auto_shift=True 时自动往后找一个空闲端口。
    """
    stdout_ready()
    lock = SingleInstance(instance_lock_path())
    if not lock.acquire():
        holder = lock.holder_pid()
        detail = f"（持有者 PID {holder}）" if holder else ""
        print(f"[错误] 校园网自连服务已有实例运行{detail}。", file=sys.stderr)
        raise SystemExit(1)

    try:
        store = store or Store()
        manager = MonitorManager()
        if auth_required is None:
            auth_required = host not in ("127.0.0.1", "localhost", "::1")
        auth = ApiAuth() if auth_required else None
        handler_cls = build_handler(
            store, manager, auth=auth, auth_required=bool(auth_required)
        )
        _install_dispatch(handler_cls)

        try:
            port = ports.resolve_ports(
                {port: "服务"}, host=host, auto_shift=auto_shift
            )[port]
        except OSError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            print(
                f"        换个端口启动：python utils/run.py serve --port {port + 20}\n"
                "        或先关掉占用它的那个程序。",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc

        try:
            httpd = ThreadingHTTPServer((host, port), handler_cls)
        except OSError as exc:
            # 预检通过但 bind 仍失败：端口在竞态窗口内被抢占
            print(f"[错误] 端口 {port} 绑定失败：{exc}", file=sys.stderr)
            raise SystemExit(1) from exc
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
            released = False
            original_shutdown = httpd.shutdown
            original_close = httpd.server_close

            def release() -> None:
                nonlocal released
                if not released:
                    released = True
                    manager.stop()
                    lock.release()

            def shutdown() -> None:
                try:
                    original_shutdown()
                finally:
                    release()

            def server_close() -> None:
                try:
                    original_close()
                finally:
                    release()

            httpd.shutdown = shutdown  # type: ignore[method-assign]
            httpd.server_close = server_close  # type: ignore[method-assign]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            return httpd

        print(f"校园网自连服务已启动： http://{host}:{port}  （Ctrl+C 停止）")
        if auth is not None:
            print(
                "局域网控制台已启用令牌保护。令牌可用 `python utils/run.py token` 查看。"
            )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n正在停止监控与服务…")
        finally:
            try:
                manager.stop()
            finally:
                try:
                    httpd.server_close()
                finally:
                    lock.release()
        return httpd
    except BaseException:
        lock.release()
        raise
