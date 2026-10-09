"""局域网控制台的令牌认证与短期会话。"""

from __future__ import annotations

import hmac
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

    def __init__(self, token_file: Path | str | None = None, token: str | None = None) -> None:
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
                return value
        except (OSError, UnicodeError):
            pass

        value = secrets.token_urlsafe(TOKEN_SIZE)
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.token_file.with_name(f".{self.token_file.name}.{secrets.token_hex(6)}.tmp")
        try:
            temp.write_text(value + "\n", encoding="utf-8")
            try:
                temp.chmod(0o600)
            except OSError:
                pass
            temp.replace(self.token_file)
            try:
                self.token_file.chmod(0o600)
            except OSError:
                pass
        finally:
            try:
                temp.unlink()
            except OSError:
                pass
        return value

    def check_token(self, candidate: str) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate, self._token)

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
        get = getattr(headers, "get", None)
        if get is None:
            return False
        authorization = str(get("Authorization", "") or "")
        candidate = ""
        if authorization.lower().startswith("bearer "):
            candidate = authorization[7:].strip()
        if not candidate:
            candidate = str(get("X-CampusNet-Token", "") or "").strip()
        if self.check_token(candidate):
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
