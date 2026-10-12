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
from urllib.parse import urljoin, urlsplit

from . import charset as charset_mod
from .paths import DEFAULT_USER_AGENT

DEFAULT_PROBES = (
    "http://www.msftconnecttest.com/connecttest.txt",
    "http://connectivitycheck.gstatic.com/generate_204",
)

# 正文明文，放行时应命中其中之一。
# RFC 8952 建议用「canary」请求：响应与预期不一致即判定被劫持。
ONLINE_MARKERS = ("microsoft connect test", "microsoft ncsi", "ok", "204")

# 门户页常见特征。命中则 302/200 都判为未认证。
PORTAL_BODY_MARKERS = (
    "eportal",
    "srun_portal",
    "drcom",
    "captive",
    "portal/login",
    "请登录",
    "上网登录",
    "用户认证",
    "网络认证",
)

# 跟随跳转的最大层数。
#
# 相对跳转可形成环；没有深度限制时，每轮探活都可能反复请求同一地址。
# 限制单次探活的请求次数，避免把监控循环变成门户的压力源。
MAX_REDIRECTS = 5


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
    （实测某高校门户在 172.26.0.0/16 段），代理根本到不了，走代理必然失败。

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


def looks_like_portal_body(text: str) -> bool:
    """正文是否是门户/认证页。"""
    if not text:
        return False
    low = text.lower()
    return any(marker in low for marker in PORTAL_BODY_MARKERS)


def probe(
    url: str,
    timeout: float = 5.0,
    insecure: bool = False,
    _depth: int = 0,
    _opener: urllib.request.OpenerDirector | None = None,
) -> ProbeResult:
    """请求一个明文探测地址；被劫持到门户页才视为未认证。

    判定依据来自 RFC 8952 与 Firefox 的 CaptiveDetect.sys.mjs：
    用「canary」请求探活，响应与预期不一致才算被劫持。

    关键区分——302 不等于掉线。实测 www.qq.com 首页就返回
    302 跳 https（正常行为），早前一律判 redirected 会误报掉线，
    触发无意义的重连。所以 302 时要先看跳转目标像不像门户。
    """
    if not url:
        url = DEFAULT_PROBES[0]
    if _depth >= MAX_REDIRECTS:
        return ProbeResult(False, "redirect_loop", f"跳转超过 {MAX_REDIRECTS} 层，判定为跳转环", url)
    # 同一次探活只建一个 opener：每层重建一次，Cookie 与处理器状态
    # 都丢在重来里，跟跳转深度叠加后纯属白烧。
    opener = _opener if _opener is not None else _build_opener(insecure)
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    try:
        with opener.open(request, timeout=timeout) as resp:
            body = _decode_body(resp.read(4096), resp.headers)
            final = resp.geturl()
            # 跟到门户页也算被劫持：某些门户直接 200 吐登录表单
            if is_portal_url(final) or looks_like_portal_body(body):
                return ProbeResult(
                    False, "portal_page", f"HTTP {resp.status} 但返回了门户页", final
                )
            return ProbeResult(True, "online", body.strip()[:120], final)
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location", "") if exc.headers else ""
        if exc.code in (301, 302, 303, 307, 308) and location:
            location = urljoin(exc.geturl() or url, location)
            # 跳到门户才是掉线；跳到同站 https 属正常跳转
            if is_portal_url(location):
                return ProbeResult(False, "redirected", f"HTTP {exc.code} -> {location}", location)
            try:
                followed = probe(location, timeout=timeout, insecure=insecure, _depth=_depth + 1, _opener=opener)
            except Exception:  # noqa: BLE001
                followed = ProbeResult(False, "redirected", f"HTTP {exc.code} -> {location}", location)
            if followed.online:
                return ProbeResult(True, "redirected_ok", f"HTTP {exc.code} -> {location}", location)
            return followed
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


def probe_any(
    urls: tuple[str, ...] = DEFAULT_PROBES,
    timeout: float = 5.0,
    insecure: bool = False,
) -> ProbeResult:
    """依次尝试多个探测地址，任一放行即视为在线。"""
    last = ProbeResult(False, "no_probe", "没有可用探测地址")
    for candidate in urls:
        last = probe(candidate, timeout=timeout, insecure=insecure)
        if last.online:
            return last
    return last


def probe_configured(
    raw: str, timeout: float = 5.0, insecure: bool = False
) -> ProbeResult:
    """按账号配置的探测地址探活；配置里可以写多个，任一放行即算在线。

    单地址与 probe() 逐字节等价，是历史默认行为；多地址是刻意的加固：
    canary 在校园网里常被 CDN 拦、被限速或被 DNS 劫持成不明确的结果，
    只挂一个地址会把「其实在线」判成「未放行」，进而产出大量
    「在线未确认」，并放大日志与请求量。

    分隔符用空格、逗号或换行都行，例如：
        http://www.msftconnecttest.com/connecttest.txt, http://captive.apple.com/hotspot-detect.html
    """
    targets = [item for item in (raw or "").replace(",", " ").split() if item]
    if not targets:
        # 留空时沿用默认 canary，与 probe() 的行为保持一致。
        return probe(raw, timeout=timeout, insecure=insecure)
    return probe_any(tuple(targets), timeout=timeout, insecure=insecure)


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
        if exc.code in (301, 302, 303, 307, 308) and location:
            location = urljoin(exc.geturl() or start_url, location)
            if urlsplit(location).scheme in ("http", "https") and is_portal_url(location):
                return location
        return None
    except Exception:  # noqa: BLE001
        return None
