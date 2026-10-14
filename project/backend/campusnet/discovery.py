"""厂商无关的门户自动发现：网关探测 → 路径扫描 → 指纹识别 → 字段提取。

设计前提：校园网门户没有统一标准，各家字段名、编码方式、提交方式都不同。
本模块不绑定任何厂商，而是给出一组高频候选路径与指纹特征，命中即用。
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from . import probe as probe_mod
from . import profiles as profiles_mod
from . import tls as tls_mod
from .paths import DEFAULT_USER_AGENT

# 高频门户路径候选：Dr.COM / 深菱 / H3C / 新网 / 华为 / 深澜 SRUN 等常见形态
PORTAL_PATHS: tuple[str, ...] = (
    "/srun_portal_pc",
    "/srun_portal",
    "/cgi-bin/get_challenge",
    "/eportal/portal/login",
    "/eportal/portal/login/",
    "/eportal/login",
    "/portal/login",
    "/portal",
    "/login",
    "/webauth/portal_login.html",
    "/webauth/auth",
    "/auth/login",
    "/sportal/authorize/index",
    "/sportal/login",
    "/index/login",
    "/login.html",
    "/cas/login",
    "/authserver/login",
    "/nms/login",
    "/cgi-bin/login.cgi",
)

# 厂商指纹与默认模式现在都由规则文件派生（profiles/ 目录，一个厂商一个 JSON）：
# 新增一所学校 = 加一条数据，不用改这里的代码。
#
# 下面三张表保留为只读视图，避免调用方与既有测试大改。顺序即优先级，由文件名
# 前缀决定：01-srun 必须排在 04-h3c-newstart 前面——有些学校的门户 URL 是
# srun_portal_pc，先命中「h3c」就会被建议 browser 模式，而它实际该走 srun。
_PORTAL_PROFILES = profiles_mod.load_profiles()

VENDOR_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    (item.name, item.all_tokens) for item in _PORTAL_PROFILES
)

VENDOR_PASSWORD_MODES: dict[str, str] = {
    item.name: item.password_mode for item in _PORTAL_PROFILES
}

VENDOR_LOGIN_MODES: dict[str, str] = {
    item.name: item.login_mode for item in _PORTAL_PROFILES
}

# 探测地址池：按顺序尝试，任一放行即视为在线
PROBE_CANDIDATES: tuple[str, ...] = (
    "http://www.msftconnecttest.com/connecttest.txt",
    "http://connectivitycheck.gstatic.com/generate_204",
    "http://www.baidu.com/",
    "http://www.qq.com/",
    "http://detectportal.firefox.com/success.txt",
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, ANN001
        return None


class _NoProxy(urllib.request.ProxyHandler):
    """强制直连，不读 http_proxy 环境变量。

    扫网关与门户都是内网地址，系统代理到不了。
    """

    def __init__(self) -> None:
        super().__init__({})


def _fetch_opener() -> urllib.request.OpenerDirector:
    # 扫的是网关与门户，都是内网地址，一律自签证书
    ctx = tls_mod.portal_context(insecure=True)
    opener = urllib.request.build_opener(
        _NoProxy(),
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ctx),
    )
    opener.addheaders = [
        ("User-Agent", DEFAULT_USER_AGENT),
        ("Accept", "text/html,application/xhtml+xml,*/*;q=0.8"),
    ]
    return opener


def _fetch(url: str, timeout: float, method: str = "GET", data: bytes | None = None,
           referer: str = "") -> tuple[int, str, str, dict[str, str]] | None:
    opener = _fetch_opener()
    if referer:
        opener.addheaders.append(("Referer", referer))
    request = urllib.request.Request(url, data=data, method=method)
    try:
        with opener.open(request, timeout=timeout) as resp:
            body = resp.read(200_000).decode("utf-8", errors="replace")
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, body, resp.geturl(), hdrs
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read(50_000).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        hdrs = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return exc.code, raw, url, hdrs
    except Exception:  # noqa: BLE001 - 探测必须永不抛异常
        return None


# ---------------------------------------------------------------- 网关发现


def local_ipv4() -> str | None:
    """不依赖外部库的取本机出口 IP 方法。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.5)
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def gateway_of(ip: str | None = None) -> str | None:
    """从本机 IP 推导默认网关。

    校园网常见的 /16 掩码会让网关落在 x.y.0.1，/24 及更窄则落在 x.y.z.1。
    这里只按 /16 与 /24 两种常见形态推，命中不了就交给 scan_gateway 扫。
    """
    if ip is None:
        ip = local_ipv4()
    if not ip:
        return None
    parts = ip.split(".")
    if len(parts) != 4:
        return None
    try:
        a, b, c, _d = (int(p) for p in parts)
    except ValueError:
        return None
    return f"{a}.{b}.0.1"


def gateway_candidates(ip: str | None = None) -> list[str]:
    """同一台机器可能的两个网关：/16 网段的 x.y.0.1 与 /24 的 x.y.z.1。"""
    if ip is None:
        ip = local_ipv4()
    if not ip:
        return []
    parts = ip.split(".")
    if len(parts) != 4:
        return []
    try:
        a, b, c, d = (int(p) for p in parts)
    except ValueError:
        return []
    out = [f"{a}.{b}.0.1"]
    narrow = f"{a}.{b}.{c}.1"
    if narrow not in out:
        out.append(narrow)
    return out


def guess_gateways() -> list[str]:
    """常见网关候选：本机推导 + 经典校园网段，顺序即优先级。"""
    out: list[str] = list(gateway_candidates())
    for extra in ("10.0.0.1", "192.168.1.1", "172.16.0.1", "10.1.1.1", "192.168.0.1"):
        if extra not in out:
            out.append(extra)
    return out


def _is_private_ipv4(ip: str) -> bool:
    """RFC 1918 私有段 + 回环：DNS 劫持到这些地址就说明域名被门户接管了。"""
    try:
        parts = [int(p) for p in ip.split(".")]
    except ValueError:
        return False
    if len(parts) != 4:
        return False
    a, b, _c, _d = parts
    return a == 10 or a == 127 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)


def detect_dns_hijack(hostname: str) -> str | None:
    """DNS 劫持判定：域名被解析到内网地址时返回那个内网 IP，否则 None。

    校园网常见做法是劫持 DNS，把任何域名都解析到门户服务器的内网 IP，
    从而把用户「拦」在认证页。这个 IP 就是最直接的门户线索。
    """
    host = (urlsplit("http://" + hostname).hostname or "").strip()
    if not host:
        return None
    # 回环与 IP 字面量都谈不上「域名被劫持」：前者就是本机，后者压根没有域名。
    # 少了这一句，seed 是回环时这里会凭空造出 http://127.0.0.1/ 去扫，
    # 于是调用方的红绿取决于那个端口有没有人应答——本机没有所以绿，CI 上有所以红。
    if host == "localhost" or host == "::1" or host.startswith("127."):
        return None
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return None
    for info in infos:
        ip = info[4][0]
        if ":" in ip:  # 只看 IPv4
            continue
        if _is_private_ipv4(ip):
            return ip
    return None


# ---------------------------------------------------------------- 指纹与字段


def identify_vendor(html_text: str, url: str = "") -> tuple[str, str]:
    """返回 (厂商, 命中的特征串)；未命中则厂商为 '未知厂商'。"""
    profile, token = profiles_mod.identify(html_text, url, _PORTAL_PROFILES)
    if profile is not None:
        return profile.name, token
    return "未知厂商", ""


_CAPTCHA_HINTS = ("captcha", "验证码", "vercode", "codeinput", "yzcode")


def needs_captcha(html_text: str) -> bool:
    low = html_text.lower()
    return any(hint in low for hint in _CAPTCHA_HINTS)


@dataclass
class PortalCandidate:
    url: str
    status: int
    title: str = ""
    vendor: str = "未知厂商"
    signature: str = ""
    login_path: str = ""
    username_field: str = ""
    password_field: str = ""
    hidden_fields: dict[str, str] = field(default_factory=dict)
    extra_fields: dict[str, str] = field(default_factory=dict)
    needs_captcha: bool = False
    suggested_login_mode: str = "http"
    suggested_password_mode: str = "plain"

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "title": self.title,
            "vendor": self.vendor,
            "signature": self.signature,
            "login_path": self.login_path,
            "username_field": self.username_field,
            "password_field": self.password_field,
            "hidden_fields": self.hidden_fields,
            "extra_fields": self.extra_fields,
            "needs_captcha": self.needs_captcha,
            "suggested_login_mode": self.suggested_login_mode,
            "suggested_password_mode": self.suggested_password_mode,
        }


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _title_of(html_text: str) -> str:
    match = _TITLE_RE.search(html_text or "")
    return html_lib_unescape(match.group(1)).strip()[:60] if match else ""


def html_lib_unescape(text: str) -> str:
    import html as html_mod

    return html_mod.unescape(text or "")


def analyze_portal(url: str, html_text: str, timeout: float = 6.0) -> PortalCandidate:
    """把一个门户页面解析成可直接用于登录的配置建议。"""
    from . import portal as portal_mod

    vendor, signature = identify_vendor(html_text, url)
    candidate = PortalCandidate(
        url=url,
        status=200,
        title=_title_of(html_text),
        vendor=vendor,
        signature=signature,
        needs_captcha=needs_captcha(html_text),
        suggested_login_mode=VENDOR_LOGIN_MODES.get(vendor, "browser"),
        suggested_password_mode=VENDOR_PASSWORD_MODES.get(vendor, "plain"),
    )

    forms = portal_mod.parse_forms(html_text)
    form = portal_mod.pick_login_form(forms)
    if form is not None:
        candidate.login_path = form.action
        candidate.username_field = form.username_field
        candidate.password_field = form.password_field
        # 隐藏字段基本都是门户要求的固定参数，直接当附加字段带上；
        # 空值隐藏字段（如 0MKKey）也必须带，否则门户会拒
        for key, value in form.fields.items():
            if key in (candidate.username_field, candidate.password_field):
                continue
            if key:
                candidate.hidden_fields[key] = value
        candidate.extra_fields = dict(candidate.hidden_fields)

    if candidate.needs_captcha:
        # 出现验证码就别指望纯 HTTP 直连了
        candidate.suggested_login_mode = "browser"
    return candidate


# ---------------------------------------------------------------- 扫描门户


def scan_gateway(
    gateway: str,
    paths: tuple[str, ...] = PORTAL_PATHS,
    timeout: float = 4.0,
    limit: int = 6,
) -> list[PortalCandidate]:
    """在网关上按候选路径扫一遍，返回可用的门户候选。

    候选路径大多会直接拒绝连接，所以对每个网关只保留少量结果就返回，
    避免在断网的校园网里空转几十秒。
    """
    found: list[PortalCandidate] = []
    for scheme in ("http", "https"):
        for path in paths:
            url = f"{scheme}://{gateway}{path}"
            got = _fetch(url, timeout=timeout)
            if got is None:
                continue
            status, body, final_url, _ = got
            if status >= 400 and not body.strip():
                continue
            candidate = analyze_portal(url, body, timeout=timeout)
            candidate.status = status
            found.append(candidate)
            if len(found) >= limit:
                return found
    return found


def scan_all_gateways(
    gateways: list[str] | None = None,
    paths: tuple[str, ...] = PORTAL_PATHS,
    timeout: float = 3.0,
    per_gateway_limit: int = 2,
    total_limit: int = 6,
) -> list[PortalCandidate]:
    """逐个网关扫描，命中立即停止，避免把时间耗在无关网段上。"""
    gateways = gateways if gateways is not None else guess_gateways()
    out: list[PortalCandidate] = []
    for gateway in gateways:
        out.extend(scan_gateway(gateway, paths=paths, timeout=timeout, limit=per_gateway_limit))
        if out:
            break
    return out[:total_limit]


def scan_urls(urls: tuple[str, ...] | list[str], timeout: float = 5.0) -> list[PortalCandidate]:
    """直接分析一组已知地址（例如从 302 跳转里拿到的）。"""
    out: list[PortalCandidate] = []
    for url in urls:
        if not url:
            continue
        got = _fetch(url, timeout=timeout)
        if got is None:
            continue
        status, body, final_url, _ = got
        candidate = analyze_portal(final_url, body, timeout=timeout)
        candidate.status = status
        out.append(candidate)
    return out


def discover(
    seed: str = "",
    timeout: float = 4.0,
    include_gateway_scan: bool = True,
) -> dict[str, Any]:
    """一站式发现：先看种子地址/探活跳转，再退回到扫网关。

    返回结构化建议，供前端一键填充，也供 CLI 直接建账号。
    """
    report: dict[str, Any] = {
        "ok": False,
        "seed": seed,
        "probe_status": "",
        "redirect_portal": "",
        "gateways": [],
        "candidates": [],
        "hints": [],
        "probe_candidates": list(PROBE_CANDIDATES),
    }

    # 种子来自 API 参数，先挡掉非 http/https：往下 probe/scan_urls 都会真的发请求，
    # file:// 这类 schemes 会把本地文件当门户页读进来。挡掉后退回默认探活地址。
    if seed and not is_url(seed):
        report["hints"].append(f"忽略非 http/https 的种子地址：{seed}")
        seed = ""
    seed = seed or PROBE_CANDIDATES[0]
    check = probe_mod.probe(seed, timeout=timeout)
    report["probe_status"] = check.status
    report["probe_detail"] = check.detail

    if not check.online:
        report["hints"].append("当前未放行，门户重定向是首选线索")
        if check.final_url and probe_mod.is_portal_url(check.final_url):
            report["redirect_portal"] = check.final_url

    seeds: list[str] = []
    if report["redirect_portal"]:
        seeds.append(report["redirect_portal"])
    detected = probe_mod.detect_portal_url(seed, timeout=timeout)
    if detected and detected not in seeds:
        seeds.append(detected)

    # DNS 劫持判定：域名被解析到内网地址，多半被门户劫持，内网 IP 即门户线索。
    seed_host = urlsplit(seed).hostname
    if seed_host:
        hijack_ip = detect_dns_hijack(seed_host)
        if hijack_ip:
            hijack_url = f"http://{hijack_ip}/"
            if hijack_url not in seeds:
                seeds.append(hijack_url)
            report["hints"].append(
                f"检测到 DNS 劫持：{seed_host} 解析到内网 {hijack_ip}"
            )

    candidates = scan_urls(seeds, timeout=timeout) if seeds else []

    if not candidates and include_gateway_scan:
        gateways = guess_gateways()
        report["gateways"] = gateways
        candidates = scan_all_gateways(
            gateways[:3], timeout=min(timeout, 3.0), per_gateway_limit=2, total_limit=6
        )

    if not candidates:
        report["hints"].append("未能自动发现门户，可在控制台手动填写门户地址与字段名")
        report["hints"].append("若门户需要 JS 渲染或验证码，请把登录方式改为「浏览器登录」")
        return report

    # 去重后按可信度排序：有登录表单 > 有标题 > 路径含 login
    seen: set[str] = set()
    unique: list[PortalCandidate] = []
    for candidate in candidates:
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        unique.append(candidate)
    unique.sort(
        key=lambda c: (
            bool(c.username_field and c.password_field),
            bool(c.title),
            "login" in c.url.lower(),
            c.status < 400,
        ),
        reverse=True,
    )

    report["candidates"] = [c.as_dict() for c in unique]
    report["ok"] = True
    best = unique[0]
    report["hints"].append(
        f"最佳候选：{best.vendor} / {best.url}"
        + (f"，需浏览器登录" if best.needs_captcha else "")
    )
    return report


def to_account_fields(candidate: dict[str, Any]) -> dict[str, Any]:
    """把候选建议转成创建账号所需的字段字典。"""
    return {
        "portal_url": candidate.get("url", ""),
        "username_field": candidate.get("username_field", ""),
        "password_field": candidate.get("password_field", ""),
        "extra_fields": candidate.get("extra_fields", {}) or {},
        "login_mode": candidate.get("suggested_login_mode", "http"),
        "password_mode": candidate.get("suggested_password_mode", "plain"),
    }


def dump(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


def is_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)
