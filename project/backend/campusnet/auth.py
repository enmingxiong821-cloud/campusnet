"""局域网控制台的令牌认证与短期会话。"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from http import cookies
from pathlib import Path

from .paths import api_token_path

SESSION_COOKIE = "campusnet_session"
SESSION_TTL = 12 * 60 * 60
TOKEN_SIZE = 32


class ApiAuth:
    """管理一个本机令牌和内存会话。

    令牌落在被 .gitignore 排除的用户数据库目录中；浏览器登录后只保存
    HttpOnly 会话 Cookie，前端不会把令牌写进 localStorage 或 URL。
    """

    def __init__(
        self, token_file: Path | str | None = None, token: str | None = None
    ) -> None:
        self.token_file = Path(token_file) if token_file else api_token_path()
        self._token = token.strip() if token else self._load_or_create()
        if not self._token:
            raise RuntimeError("API 令牌不能为空")
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def token(self) -> str:
        return self._token

    def _load_or_create(self) -> str:
        try:
            value = self.token_file.read_text(encoding="utf-8").strip()
            if len(value) >= 32:
                self._restrict_permissions()
                return value
        except (OSError, UnicodeError):
            pass

        value = secrets.token_urlsafe(TOKEN_SIZE)
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.token_file.with_name(
            f".{self.token_file.name}.{secrets.token_hex(6)}.tmp"
        )
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            with os.fdopen(
                os.open(temp, flags, 0o600), "w", encoding="utf-8"
            ) as stream:
                stream.write(value + "\n")
            temp.replace(self.token_file)
            self._restrict_permissions()
        finally:
            try:
                temp.unlink()
            except OSError:
                pass
        return value

    def _restrict_permissions(self) -> None:
        """POSIX 上必须收紧已有令牌；Windows 文件权限由 ACL 管理。"""
        if os.name != "posix":
            return
        try:
            self.token_file.chmod(0o600)
        except OSError as exc:
            raise RuntimeError(
                f"API 令牌文件权限无法收紧：{self.token_file}。"
                "请确认文件归当前运行用户所有，并将权限设置为 0600 后重新启动；"
                "不要通过关闭认证绕过此错误。"
            ) from exc

    def check_token(self, candidate: str) -> bool:
        if not candidate:
            return False
        # hmac.compare_digest 对含非 ASCII 的 str 直接抛 TypeError（HTTP 头按
        # latin-1 解码，一个高位字节就能触发）。而鉴权门禁在 _dispatch 的 try
        # 之外，异常逃出去就是连接被重置 + stderr 一整段 traceback，客户端连
        # 401 都拿不到——发个畸形令牌头就能反复刷日志、耗线程。
        # 统一编码成 UTF-8 字节再比：长度不等时 compare_digest 自己会短路。
        return hmac.compare_digest(
            candidate.encode("utf-8"), self._token.encode("utf-8")
        )

    @staticmethod
    def _token_from_headers(headers: object) -> str:
        """从请求头取出「显式令牌」；取不到返回空串。"""
        get = getattr(headers, "get", None)
        if get is None:
            return ""
        authorization = str(get("Authorization", "") or "")
        if authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return str(get("X-CampusNet-Token", "") or "").strip()

    def explicit_token_ok(self, headers: object) -> bool:
        """只认显式令牌，不认会话 Cookie。

        同源校验的豁免必须用它，不能用 authenticated()：Cookie 由浏览器自动附带，
        正是 CSRF 的成因；而 Authorization / X-CampusNet-Token 这类自定义头，跨站页
        既读不到、真要发也会先触发 CORS 预检（本服务的预检不放行跨站来源），
        所以「令牌有效」才配当豁免依据。注意是校验**有效性**，不是「带了头就放行」。
        """
        return self.check_token(self._token_from_headers(headers))

    def _session_from_headers(self, headers: object) -> str:
        get = getattr(headers, "get", None)
        if get is None:
            return ""
        raw = str(get("Cookie", "") or "")
        jar = cookies.SimpleCookie()
        try:
            jar.load(raw)
        except cookies.CookieError:
            return ""
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel else ""

    def authenticated(self, headers: object) -> bool:
        if getattr(headers, "get", None) is None:
            return False
        if self.check_token(self._token_from_headers(headers)):
            return True

        session = self._session_from_headers(headers)
        if not session:
            return False
        now = time.time()
        with self._lock:
            expires = self._sessions.get(session)
            if expires is None:
                return False
            if expires <= now:
                self._sessions.pop(session, None)
                return False
            return True

    def login(self, candidate: str) -> str | None:
        if not self.check_token(candidate.strip()):
            return None
        session = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[session] = time.time() + SESSION_TTL
        return session

    def issue_session(self) -> str:
        """直接发放一个会话，不必再验令牌——扫码配对成功后走这里。"""
        session = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[session] = time.time() + SESSION_TTL
        return session

    def logout(self, headers: object) -> None:
        session = self._session_from_headers(headers)
        if session:
            with self._lock:
                self._sessions.pop(session, None)

    @staticmethod
    def set_cookie(session: str, max_age: int = SESSION_TTL) -> str:
        return (
            f"{SESSION_COOKIE}={session}; Max-Age={max_age}; Path=/; "
            "HttpOnly; SameSite=Strict"
        )

    @staticmethod
    def clear_cookie() -> str:
        return f"{SESSION_COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"
