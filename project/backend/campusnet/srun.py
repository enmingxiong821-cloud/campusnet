"""深澜 SRUN 认证：challenge + HMAC-MD5 + xxtea 加密。

西农大用的 `srun_portal_pc` 属于这一类，与 Dr.COM 的表单登录完全不同：
页面里没有 `<form>`，登录由三个接口串成

    1. GET /cgi-bin/get_challenge      → challenge token（60 秒有效）
    2. 本地算 hmd5 / info / chksum    → 三段密文
    3. GET /cgi-bin/srun_portal        → 带密文提交登录

已实测西农大门户（http://172.26.8.11）暴露 get_challenge 且能取到 token。

三段密文的算法（公开逆向资料，参数与实测抓包一致）：

    hmd5    = HMAC-MD5(key=challenge, msg=password)
    info    = "{SRBX1}" + xencode_b64(json{username,password,ip,acid,enc_ver}, challenge)
    chksum  = SHA1(challenge+username + challenge+hmd5 + challenge+ac_id
                   + challenge+ip + challenge+n + challenge+type + challenge+info)

顺序不能错：chksum 依赖 info 与 hmd5。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .charset import decode as decode_body
from .paths import DEFAULT_USER_AGENT

# SRUN 加密常量，三段密文都依赖它们，错一个就是 sign_error
DEFAULT_AC_ID = "1"
DEFAULT_N = "200"
DEFAULT_TYPE = "1"
ENC_VER = "srun_bx1"

CHALLENGE_PATH = "/cgi-bin/get_challenge"
AUTH_PATH = "/cgi-bin/srun_portal"

# 成功标志：res=ok 且无 error
SUCCESS_MARKERS = ("login_ok", "e0000")
ALREADY_ONLINE_MARKERS = ("ip_already_online",)
# 明确可读的失败原因
FAILURE_MARKERS = {
    "E0000": "门户返回成功但同时报错",
    "E2620": "超出允许的在线设备数",
    "user_denied": "账号或密码错误",
    "password_error": "密码错误",
    "ldap_error": "账号或密码错误",
    "no_response_data_error": "门户无响应数据",
    "not_online_error": "会话异常，需重新登录",
    "sign_error": "签名校验失败（ac_id 或加密算法与门户版本不匹配）",
    "RD000": "门户要求人工处理",
}


class SrunError(Exception):
    """SRUN 认证失败。message 面向用户，已译成中文。"""


# ---------------------------------------------------------------- xxtea


def _mx(sum_val: int, y: int, z: int, p: int, e: int, key: list[int]) -> int:
    return (
        (
            ((z >> 5) ^ (y << 2))
            + ((y >> 3) ^ (z << 4))
        )
        ^ ((sum_val ^ y) + (key[(p & 3) ^ e] ^ z))
    ) & 0xFFFFFFFF


# XXTEA 的轮数随分组数变化，不是固定 32。
# 固定 32 的话只有 n=2 碰巧对（6+52//2=32），n 一大就与门户算出的密文不一致，
# 结果是 sign_error。
_DELTA = 0x9E3779B9


def _rounds_for(n: int) -> int:
    """轮数随分组数变化，公式来自 Portal.js：`floor(6 + 52/(n+1))`。

    我最初写成 `6 + 52//n`，只有 n=2 时两边都是 32 才碰巧对。
    括号位置差一位，n 一大就与门户算出的密文不同，表现为 auth_info_error。
    """
    return 6 + 52 // (n + 1)


def xxtea_encrypt(data: bytes, key: bytes) -> bytes:
    """XXTEA 分组加密，SRUN 用它保护 info 字段。

    严格照 Wheeler/Needham 的 BTea 结构写：加密侧跨轮保留 z，
    解密侧跨轮保留 y。这两者互为逆运算。
    把 z 换成每轮重算的 v[p+1] 虽然自洽，但与门户算出的密文不同，
    提交后门户只会回 sign_error。
    """
    # 打包：每 4 字节小端拼成一个 uint32，不足一 word 的尾部零填充。
    #
    # Portal.js 的 s() 函数末尾还会追加一个 word 记录原长度，但具体记什么
    # （len%4 还是完整 len）、以及尾部不足一 word 的字节如何保存，我没有
    # 读到源码，无法确认。早期版本照抄「追加余数」的做法，那个 word 会覆盖
    # 原文最后几个字节，导致 decrypt 还原不出原文——是自造的缺陷。
    #
    # 现在改为纯零填充：至少往返可逆、语义明确。
    # 未对齐时与 Portal.js 的差异是真实存在的风险，见 encode_info 的说明。
    # 门户若回 auth_info_error，这里是第一嫌疑点，只有抓包才能定论。
    padded = data + b"\x00" * (-len(data) % 4)
    if len(padded) < 8:
        padded = padded + b"\x00" * (8 - len(padded))
    n = len(padded) // 4
    v = list(struct.unpack("<%dI" % n, padded))
    k = list(struct.unpack("<4I", key[:16].ljust(16, b"\x00")))

    rounds = _rounds_for(n)
    sum_val = 0
    z = v[n - 1]
    for _ in range(rounds):
        sum_val = (sum_val + _DELTA) & 0xFFFFFFFF
        e = (sum_val >> 2) & 3
        for p in range(n - 1):
            y = v[p + 1]
            v[p] = (v[p] + _mx(sum_val, y, z, p, e, k)) & 0xFFFFFFFF
            z = v[p]
        y = v[0]
        v[n - 1] = (v[n - 1] + _mx(sum_val, y, z, n - 1, e, k)) & 0xFFFFFFFF
        z = v[n - 1]

    return struct.pack("<%dI" % n, *v)


def xxtea_decrypt(data: bytes, key: bytes) -> bytes:
    """解密，不做任何长度裁剪。

    与 encrypt 互为逆运算：输出末尾可能含一个记录余数的 word。
    长度非 4 整数倍时，encrypt 末尾追加的余数 word 会覆盖原文尾部字节，
    该情形下 decrypt[:len(原文)] 不等于原文——这是当前打包方案的已知缺陷，
    详见 xxtea_encrypt 的注释。只有 4 字节对齐的输入能保证往返一致。
    """
    n = len(data) // 4
    if n < 2:
        return b""
    v = list(struct.unpack("<%dI" % n, data))
    k = list(struct.unpack("<4I", key[:16].ljust(16, b"\x00")))

    rounds = _rounds_for(n)
    sum_val = (rounds * _DELTA) & 0xFFFFFFFF
    # y 跨轮保留：上一轮的新 v[0] 就是下一轮第一次迭代的 y
    y = v[0]
    for _ in range(rounds):
        e = (sum_val >> 2) & 3
        for p in range(n - 1, 0, -1):
            z = v[p - 1]
            v[p] = (v[p] - _mx(sum_val, y, z, p, e, k)) & 0xFFFFFFFF
            y = v[p]
        z = v[n - 1]
        v[0] = (v[0] - _mx(sum_val, y, z, 0, e, k)) & 0xFFFFFFFF
        y = v[0]
        sum_val = (sum_val - _DELTA) & 0xFFFFFFFF

    return struct.pack("<%dI" % n, *v)


# ---------------------------------------------------------------- 换表 Base64

# SRUN 的 Base64 字母表被整体换过，与标准表毫无关系。
#
# 我最初误用了标准表（ABCDEF...+/），那等于没换表——标准库能编出来，
# 但门户解不开，只会回 auth_info_error。字母表经 4 份独立公开实现交叉确认：
#   Mythologyli/zju-web-login、HollowMan6/Srun-LZU-Network-Auth、
#   caterpie 抓包分析、jztxt.com 的 C++ 转写版
# 注意第 33 位是 /、第 34 位是 3，与标准表的 /+9 位置不同。
_SRUN_ALPHABET = (
    "LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA"
)
_STD_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
)


def srun_b64(data: bytes) -> str:
    """换表 Base64 编码。"""
    std = base64.b64encode(data).decode("ascii")
    return std.translate(str.maketrans(_STD_ALPHABET, _SRUN_ALPHABET))


def srun_unb64(text: str) -> bytes:
    return base64.b64decode(text.translate(str.maketrans(_SRUN_ALPHABET, _STD_ALPHABET)))


# ---------------------------------------------------------------- 三段密文


def hmd5(password: str, token: str) -> str:
    """HMAC-MD5，key 是 challenge，msg 是密码。

    注意方向：不是普通 MD5(key=密码, msg=challenge)。
    """
    return hmac.new(token.encode("utf-8"), password.encode("utf-8"), hashlib.md5).hexdigest()


def encode_info(
    username: str,
    password: str,
    ip: str,
    ac_id: str,
    token: str,
    enc_ver: str = ENC_VER,
) -> str:
    """info 字段：xxtea 加密用户信息后换表 Base64，前缀 {SRBX1}。"""
    payload = {
        "username": username,
        "password": password,
        "ip": ip,
        "acid": ac_id,
        "enc_ver": enc_ver,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    key = (token + enc_ver).encode("utf-8")[:16].ljust(16, b"\x00")
    return "{SRBX1}" + srun_b64(xxtea_encrypt(raw, key))


def chkstr(
    token: str,
    username: str,
    hmd5_value: str,
    ac_id: str,
    ip: str,
    n: str = DEFAULT_N,
    type_: str = DEFAULT_TYPE,
    info: str = "",
) -> str:
    """chksum 的输入拼接串。顺序固定，错一位就是 sign_error。"""
    return (
        token + username
        + token + hmd5_value
        + token + ac_id
        + token + ip
        + token + n
        + token + type_
        + token + info
    )


def compute_chksum(chk: str) -> str:
    return hashlib.sha1(chk.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- JSONP


_JSONP_RE = re.compile(r"^[^(]*\((.*)\)[;\s]*$", re.S)


def parse_jsonp(text: str) -> dict[str, Any]:
    """剥掉 JSONP 包装，解析成 dict。"""
    raw = (text or "").strip()
    match = _JSONP_RE.match(raw)
    if match:
        raw = match.group(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SrunError(f"门户返回的不是合法 JSON：{raw[:120]}") from exc
    if not isinstance(data, dict):
        raise SrunError(f"门户返回格式异常：{str(data)[:120]}")
    return data


def interpret(data: dict[str, Any]) -> tuple[bool, str]:
    """把门户响应翻译成 (是否成功, 面向用户的中文原因)。"""
    text = json.dumps(data, ensure_ascii=False)
    error = str(data.get("error", "")).lower()
    error_msg = str(data.get("error_msg", ""))

    for marker in ALREADY_ONLINE_MARKERS:
        if marker in text.lower():
            return True, "该 IP 已在在线设备列表中"

    if any(marker in text for marker in SUCCESS_MARKERS):
        return True, str(data.get("ploy_msg") or data.get("suc_msg") or "登录成功")

    # error 字段本身就是「ok」时也算成功。
    # 早期版本只判空串，门户返回 {"error":"ok","res":"ok"} 会被误判为失败——
    # 而这恰好是西农大登录成功时的标准响应格式。
    if error in ("", "ok") and str(data.get("res", "")).lower() == "ok":
        return True, str(data.get("suc_msg") or "登录成功")

    # error 字段或错误码里都能翻出原因
    for code, reason in FAILURE_MARKERS.items():
        if code.lower() in text.lower() or code.lower() in error:
            return False, reason

    return False, error_msg or error or f"门户返回 {text[:100]}"


# ---------------------------------------------------------------- 网络


@dataclass
class SrunConfig:
    base_url: str
    username: str
    password: str
    ip: str
    ac_id: str = DEFAULT_AC_ID
    n: str = DEFAULT_N
    type: str = DEFAULT_TYPE
    enc_ver: str = ENC_VER
    timeout: float = 10.0
    insecure: bool = False

    @property
    def challenge_url(self) -> str:
        return self.base_url.rstrip("/") + CHALLENGE_PATH

    @property
    def auth_url(self) -> str:
        return self.base_url.rstrip("/") + AUTH_PATH


class _NoProxy(urllib.request.ProxyHandler):
    """强制直连，不读 http_proxy 环境变量。

    SRUN 的认证服务器是内网地址（西农大 172.26.8.11），
    走系统代理必然到不了。同类项目普遍这么处理。
    """

    def __init__(self) -> None:
        super().__init__({})


def _opener(insecure: bool):
    import ssl

    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        _NoProxy(),
        urllib.request.HTTPSHandler(context=ctx),
    )
    opener.addheaders = [
        ("User-Agent", DEFAULT_USER_AGENT),
        ("Accept", "*/*"),
    ]
    return opener


def _get(opener, url: str, params: dict[str, str], referer: str, timeout: float) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(f"{url}?{query}")
    request.add_header("Referer", referer)
    try:
        with opener.open(request, timeout=timeout) as resp:
            body = decode_body(resp.read(200_000), content_type=resp.headers.get("Content-Type"))
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read(50_000)
        except Exception:  # noqa: BLE001
            pass
        body = decode_body(raw, content_type=(exc.headers or {}).get("Content-Type"))
    except Exception as exc:  # noqa: BLE001 - 网络异常统一转 SrunError
        raise SrunError(f"连不上认证服务器（{type(exc).__name__}）") from exc
    return parse_jsonp(body)


def get_challenge(config: SrunConfig) -> str:
    """第一步：取 challenge token。有效期由门户给，通常 60 秒。"""
    referer = f"{config.base_url.rstrip('/')}/srun_portal_pc?ac_id={config.ac_id}&theme=pro"
    data = _get(
        _opener(config.insecure),
        config.challenge_url,
        {
            "callback": f"jQuery{int(time.time() * 1000)}",
            "username": config.username,
            "ip": config.ip,
            "_": str(int(time.time() * 1000)),
        },
        referer,
        config.timeout,
    )
    token = str(data.get("challenge", "")).strip()
    if not token:
        raise SrunError(f"门户未下发 challenge（error={data.get('error', '?')}）")
    return token


def srun_login(config: SrunConfig, on_progress=None) -> tuple[bool, str]:
    """完整三步登录。返回 (是否成功, 中文原因)。"""
    emit = on_progress or (lambda stage, msg: None)

    emit("connect", f"正在向 {config.base_url} 取 challenge")
    token = get_challenge(config)
    emit("parse", f"已取到 challenge（前 8 位 {token[:8]}…）")

    h = hmd5(config.password, token)
    info = encode_info(
        config.username, config.password, config.ip, config.ac_id, token, config.enc_ver
    )
    chksum = compute_chksum(
        chkstr(token, config.username, h, config.ac_id, config.ip, config.n, config.type, info)
    )
    emit("submit", "三段密文已算好（hmd5 / info / chksum），提交登录")

    referer = f"{config.base_url.rstrip('/')}/srun_portal_pc?ac_id={config.ac_id}&theme=pro"
    data = _get(
        _opener(config.insecure),
        config.auth_url,
        {
            "callback": f"jQuery{int(time.time() * 1000)}",
            "action": "login",
            "username": config.username,
            "password": "{MD5}" + h,
            "os": "Windows 10",
            "name": "windows",
            "nas_ip": "",
            "double_stack": "0",
            "chksum": chksum,
            "info": info,
            "ac_id": config.ac_id,
            "ip": config.ip,
            "n": config.n,
            "type": config.type,
            "captchaVal": "",
            "_": str(int(time.time() * 1000)),
        },
        referer,
        config.timeout,
    )

    ok, reason = interpret(data)
    emit("done" if ok else "fail", reason)
    return ok, reason


def srun_logout(config: SrunConfig, on_progress=None) -> tuple[bool, str]:
    """注销。部分门户要求带 chksum，缺失时返回 false 而非崩溃。"""
    emit = on_progress or (lambda stage, msg: None)
    emit("connect", "正在注销")
    referer = f"{config.base_url.rstrip('/')}/srun_portal_pc?ac_id={config.ac_id}&theme=pro"
    try:
        data = _get(
            _opener(config.insecure),
            config.auth_url,
            {
                "callback": f"jQuery{int(time.time() * 1000)}",
                "action": "logout",
                "username": config.username,
                "ac_id": config.ac_id,
                "ip": config.ip,
                "_": str(int(time.time() * 1000)),
            },
            referer,
            config.timeout,
        )
    except SrunError as exc:
        emit("fail", str(exc))
        return False, str(exc)
    ok = str(data.get("res", "")).lower() == "ok" or data.get("error") in ("", "ok")
    emit("done" if ok else "fail", "已注销" if ok else "门户拒绝注销")
    return ok, "已注销" if ok else "门户拒绝注销"


def is_srun_portal(url: str) -> bool:
    """地址看起来像 SRUN 门户。"""
    low = (url or "").lower()
    return "srun_portal" in low or "get_challenge" in low