"""HTTP 探活：判断当前是否已被校园网认证放行。"""

from __future__ import annotations

import gzip
import io
import socket
import ssl
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from . import charset as charset_mod
from .paths import DEFAULT_USER_AGENT

DEFAULT_PROBES = (
    "http://www.msftconnecttest.com/connecttest.txt",
    "http://connectivitycheck.gstatic.com/generate_204",
)

ONLINE_MARKERS = ("microsoft connect test", "ok", "204")


@dataclass
class ProbeResult:
    online: bool
    status: str
    detail: str = ""
    final_url: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - 便利方法
        return self.online


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, ANN001
        return None


class _NoProxy(urllib.request.ProxyHandler):
    """强制直连，不读 http_proxy / HTTP_PROXY 环境变量。

    实测：设了 `http_proxy=http://127.0.0.1:9` 后，探活返回
    `unreachable`——请求被代理接管了。而门户地址是内网 IP
    （西农大是 172.26.8.11），代理根本到不了，走代理必然失败。

    同类项目的通行做法：guet-drcom 每次请求都加 `--noproxy '*'`，
    zju-web-login 用 `session.trust_env = False`。
    """

    def __init__(self) -> None:
        super().__init__({})


def _build_opener(insecure: bool) -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    handlers = [
        _NoProxy(),
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ctx),
    ]
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = [("User-Agent", DEFAULT_USER_AGENT), ("Accept", "*/*")]
    return opener


def _decode_body(raw: bytes, headers: Any = None) -> str:
    """解压后按页面声明的字符集解码，中文门户大量使用 GBK。"""
    if not raw:
        return ""
    encoding = ""
    content_type = ""
    if headers is not None:
        encoding = (headers.get("Content-Encoding") or headers.get("content-encoding") or "").lower()
        content_type = headers.get("Content-Type") or headers.get("content-type") or ""
    try:
        if "gzip" in encoding:
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        elif "deflate" in encoding:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except OSError:
        pass
    return charset_mod.decode(raw, content_type=content_type)


def probe(url: str, timeout: float = 5.0, insecure: bool = False) -> ProbeResult:
    """请求一个明文探测地址；被重定向到门户登录页即视为未认证。"""
    if not url:
        url = DEFAULT_PROBES[0]
    opener = _build_opener(insecure)
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    try:
        with opener.open(request, timeout=timeout) as resp:
            body = _decode_body(resp.read(4096), resp.headers)
            return ProbeResult(True, "online", body.strip()[:120], resp.geturl())
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location", "") if exc.headers else ""
        if exc.code in (301, 302, 303, 307, 308) and location:
            return ProbeResult(False, "redirected", f"HTTP {exc.code} -> {location}", location)
        if exc.code in (204, 200):
            return ProbeResult(True, "online", f"HTTP {exc.code}", url)
        return ProbeResult(False, "http_error", f"HTTP {exc.code}", url)
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLError):
            return ProbeResult(False, "tls_error", str(reason), url)
        if isinstance(reason, socket.timeout):
            return ProbeResult(False, "timeout", "探测超时", url)
        return ProbeResult(False, "unreachable", str(reason), url)
    except (TimeoutError, socket.timeout):
        return ProbeResult(False, "timeout", "探测超时", url)
    except Exception as exc:  # noqa: BLE001 - 探活必须永不抛异常
        return ProbeResult(False, "error", f"{type(exc).__name__}: {exc}", url)


def is_portal_url(url: str) -> bool:
    lowered = url.lower()
    return any(token in lowered for token in ("login", "eportal", "portal", "auth", "logon", "drcom"))


def probe_any(urls: tuple[str, ...] = DEFAULT_PROBES, timeout: float = 5.0) -> ProbeResult:
    """依次尝试多个探测地址，任一放行即视为在线。"""
    last = ProbeResult(False, "no_probe", "没有可用探测地址")
    for candidate in urls:
        last = probe(candidate, timeout=timeout)
        if last.online:
            return last
    return last


def detect_portal_url(start_url: str, timeout: float = 5.0) -> str | None:
    """从普通网址的 302 跳转里找出认证门户地址。"""
    parts = urlsplit(start_url)
    if parts.scheme not in ("http", "https"):
        return None
    opener = _build_opener(insecure=parts.scheme == "https")
    request = urllib.request.Request(start_url, headers={"Cache-Control": "no-cache"})
    try:
        with opener.open(request, timeout=timeout) as resp:
            return resp.geturl() if is_portal_url(resp.geturl()) else None
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location", "") if exc.headers else ""
        if location and is_portal_url(location):
            return location
        return None
    except Exception:  # noqa: BLE001
        return None