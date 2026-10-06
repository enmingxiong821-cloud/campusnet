"""深澜 SRUN 认证：challenge + HMAC-MD5 + xxtea 加密。

西农大用的 `srun_portal_pc` 属于这一类，与 Dr.COM 的表单登录完全不同：
页面里没有 `<form>`，登录由三个接口串成

    1. GET /cgi-bin/get_challenge      → challenge token（60 秒有效）
    2. 本地算 hmd5 / info / chksum    → 三段密文
    3. GET /cgi-bin/srun_portal        → 带密文提交登录

已实测西北农林科技大学的门户暴露 get_challenge 且能取到 token。

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
# 免凭据的在线状态查询：Portal.js 登录成功后轮询它判断是否跳成功页
AUTH_STATE_PATH = "/cgi-bin/rad_user_info"

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
    """XXTEA 的 MX 混合函数，分组必须与 Portal.js 完全一致。

    Portal.js 分三步累加：
        m  = z >>> 5 ^ y << 2;
        m += y >>> 3 ^ z << 4 ^ (d ^ y);
        m += k[p & 3 ^ e] ^ z;

    我最初写成 `(A + B) ^ (C + D)`，Python 里 `+` 优先级高于 `^`，
    于是算成异或整个和。加密与解密共用同一个错函数，往返测试照样全绿，
    但门户算出的密文完全不同——这批假绿测试锁死了错误实现。
    """
    m = (z >> 5) ^ ((y << 2) & 0xFFFFFFFF)
    m += (y >> 3) ^ ((z << 4) & 0xFFFFFFFF) ^ (sum_val ^ y)
    m += key[(p & 3) ^ e] ^ z
    return m & 0xFFFFFFFF


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


def _text_units(text: str) -> list[int]:
    """字符串 -> UTF-16 码元序列，对应 Portal.js 的 charCodeAt。

    这里必须是码元而不是 UTF-8 字节：中文口令在 UTF-8 下占 3 字节，
    但 JS 侧只算 1 个码元，密文长度对不上，门户解出来就是乱码。
    """
    raw = text.encode("utf-16-le", "surrogatepass")
    return [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]


def _pack_units(units: list[int]) -> list[int]:
    """每 4 个码元拼一个 word，逐个左移 8 位后按位或。

    这里是 Portal.js s() 的逐行照抄，不是「每码元占一字节」。
    区别很要紧：码元可能是 0..0xFFFF，`u1 << 8` 会一直顶到第 23 位，
    和 `u2 << 16` 的位段交叠。按位或之下结果仍与 JS 一致，
    但若改成按字节拼，中文口令的密文就跟门户算出的不是同一条。

    越界取 0 是对的：JS 里 charCodeAt 越界返回 NaN，
    而 `NaN << 8` 同样是 0。

    注意这里会把 >0xFF 的码元高位挤掉（32 位截断），门户的 l() 同样
    每个 word 只取 4 个低字节，JS 自己也还原不回去。中文口令能用的
    前提是明文保持纯 ASCII —— 由 json.dumps(ensure_ascii=True) 转成
    反斜杠 uXXXX 转义来保证，见 encode_info 的说明。
    """
    words = []
    n = len(units)
    for i in range(0, n, 4):
        w = 0
        for shift in (0, 8, 16, 24):
            j = i + shift // 8
            if j < n:
                w |= units[j] << shift
        words.append(w & 0xFFFFFFFF)
    return words


def xxtea_encrypt_text(text: str, key: bytes | list[int]) -> bytes:
    """按 Portal.js s(str, true) + encode() 加密字符串，供 info 字段使用。

    长度 word 不可省。门户 l(a, true) 的校验是
    `m = a[d-1]; if (m < c-3 || m > c) return null`，其中 c = (d-1)<<2。
    末 word 不是明文码元数就必定落在区间外，门户直接解不开 info，
    回 auth_info_error。这正是早期零填充实现的失败原因。
    """
    units = _text_units(text)
    v = _pack_units(units)
    # 追加码元总数；它不参与加密（encode() 里 n = v.length - 1），
    # 只作为长度标记原样留在密文末尾
    v.append(len(units))
    return _encrypt_words(v, key)


def xxtea_decrypt_text(data: bytes, key: bytes | list[int]) -> str | None:
    """解密 info 并还原成字符串，形状与 Portal.js l(a, true) 一致。

    校验不通过时返回 None —— 对应门户的 return null。
    """
    total = len(data) // 4
    if total < 2:
        return None
    # 末 word 是长度标记，但它同样被加密过，必须一起解密再读出来。
    # 只解前 total-1 个 word 会让轮数取值整体错位。
    words = _decrypt_words(list(struct.unpack("<%dI" % total, data)), key)
    c = (total - 1) << 2
    m = words[total - 1]
    if m <= 0 or m > c:
        return None
    units: list[int] = []
    for w in words[: total - 1]:
        units.extend((w & 0xFF, (w >> 8) & 0xFF, (w >> 16) & 0xFF, (w >> 24) & 0xFF))
    raw = bytearray()
    for u in units[:m]:
        raw.append(u & 0xFF)
        raw.append((u >> 8) & 0xFF)
    try:
        return bytes(raw).decode("utf-16-le")
    except UnicodeDecodeError:
        return None


def xxtea_encrypt(data: bytes, key: bytes) -> bytes:
    """字节流版加密，保留给内部往返测试与通用调用。

    与 xxtea_encrypt_text 的唯一差别：这里把 1 字节当 1 个码元，
    因为传入的不是 JS 字符串。协议字段请一律用 text 版本。
    """
    v = _pack_units(list(data))
    v.append(len(data))
    return _encrypt_words(v, key)


def _key_words(key: bytes | list[int]) -> list[int]:
    """把 key 归一成 4 个 uint32 word。

    Portal.js 的 k 本来就是 `s(key, false)` 得到的 word 数组，
    所以既接受 bytes 也接受已派生的 word 列表。bytes 路径保留给
    通用 XXTEA 调用（内部测试按字节喂 key），不足 16 字节补 0。
    """
    if not isinstance(key, (bytes, bytearray)):
        words = list(key)
        return (words + [0, 0, 0, 0])[:4]
    raw = bytes(key)[:16].ljust(16, b"\x00")
    return [
        raw[i] | (raw[i + 1] << 8) | (raw[i + 2] << 16) | (raw[i + 3] << 24)
        for i in range(0, 16, 4)
    ]


def _encrypt_words(v: list[int], key: bytes | list[int]) -> bytes:
    """XXTEA 加密，v 含末尾长度 word。

    循环范围必须是 range(n) 而不是 range(n-1)：Portal.js 的
    `for (p = 0; p < n; p++)` 覆盖 0..n-1，再由尾行处理 v[n]，
    合起来正好把长度 word 也加密。少一轮 v[n-1] 就漏了。
    """
    n = len(v) - 1
    k = _key_words(key)

    rounds = _rounds_for(n)
    sum_val = 0
    z = v[n]
    for _ in range(rounds):
        sum_val = (sum_val + _DELTA) & 0xFFFFFFFF
        e = (sum_val >> 2) & 3
        for p in range(n):
            y = v[p + 1]
            v[p] = (v[p] + _mx(sum_val, y, z, p, e, k)) & 0xFFFFFFFF
            z = v[p]
        y = v[0]
        v[n] = (v[n] + _mx(sum_val, y, z, n, e, k)) & 0xFFFFFFFF
        z = v[n]

    return struct.pack("<%dI" % (n + 1), *v)


def xxtea_decrypt(data: bytes, key: bytes) -> bytes:
    """字节流版解密。与 xxtea_encrypt 互为逆运算，末尾多出的长度标记已剥离。"""
    total = len(data) // 4
    # 末 word 是长度标记，但它同样被加密过，必须一起解密再丢弃
    if total < 2:
        return b""
    words = _decrypt_words(list(struct.unpack("<%dI" % total, data)), key)
    m = words[total - 1]
    c = (total - 1) << 2
    if m <= 0 or m > c:
        return b""
    return struct.pack("<%dI" % (total - 1), *words[: total - 1])[:m]


def _decrypt_words(v: list[int], key: bytes | list[int]) -> list[int]:
    """解密全部 word（含末尾长度标记），与 _encrypt_words 严格互逆。"""
    n = len(v) - 1
    k = _key_words(key)

    rounds = _rounds_for(n)
    sum_val = (rounds * _DELTA) & 0xFFFFFFFF
    # y 跨轮保留：上一轮的新 v[0] 就是下一轮第一次迭代的 y
    y = v[0]
    for _ in range(rounds):
        e = (sum_val >> 2) & 3
        for p in range(n, 0, -1):
            z = v[p - 1]
            v[p] = (v[p] - _mx(sum_val, y, z, p, e, k)) & 0xFFFFFFFF
            y = v[p]
        z = v[n]
        v[0] = (v[0] - _mx(sum_val, y, z, 0, e, k)) & 0xFFFFFFFF
        y = v[0]
        sum_val = (sum_val - _DELTA) & 0xFFFFFFFF

    return v


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
    """info 字段：xxtea 加密用户信息后换表 Base64，前缀 {SRBX1}。

    必须走 text 版本：Portal.js 用 charCodeAt 按 UTF-16 码元打包，
    早期版本把 JSON 当 UTF-8 字节塞进去，门户解出来长度对不上。
    key 由裸 token 按 UTF-16 码元打包后取前 4 个 word，见 _token_key_words。
    enc_ver 只出现在加密明文里，不参与 key。
    """
    payload = {
        "username": username,
        "password": password,
        "ip": ip,
        "acid": ac_id,
        "enc_ver": enc_ver,
    }
    # ensure_ascii=True 是必须的，不能图好看改成 False。
    # Portal.js 的 l() 从每个 word 只取 4 个低字节，而 _pack_units 照它把
    # 4 个 UTF-16 码元挤进一个 word：码元一旦 >0xFF，高位就被 32 位截断。
    # 写 ensure_ascii=False 时中文口令会往明文塞 4 个 >0xFF 码元，
    # 还原后 JSON 直接不成形（实测报 Expecting ',' delimiter），
    # 门户侧就是 auth_info_error。
    #
    # 早先注释写"这是协议本身的性质，中文口令会被门户拒"——错的。
    # 协议本身能处理中文，前提是明文保持纯 ASCII，也就是
    # 让 JSON 把中文转成 \uXXXX 转义，这正是浏览器 JSON.stringify 的默认行为。
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    key = _token_key_words(token)
    return "{SRBX1}" + srun_b64(xxtea_encrypt_text(text, key))


def _token_key_words(token: str) -> list[int]:
    """按 Portal.js 派生 key：s(token, false) 取前 4 个 word，短了补 0。

    Portal.js:758 的 key 就是裸 token —— `encode(info, token)`，
    enc_ver 只是塞在加密明文里，不参与 key。早先写成
    `(token + enc_ver)[:16]`：token 长度 ≥16 时两者恰好等价，
    短 token 时会把 enc_ver 灌进 word[2]/word[3]，密文必然不同。
    实测 get_challenge 下发 32/64 位 hex token，所以真实登录一直没踩到，
    但这属于埋雷：token 生成策略一变就会变成 auth_info_error。

    Portal.js:700 的 `if (k.length < 4) k.length = 4` 靠稀疏数组补 undefined，
    参与运算时等价于 0，这里显式补 0。
    """
    words = _pack_units(_text_units(token))
    while len(words) < 4:
        words.append(0)
    return words[:4]


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

    SRUN 的认证服务器是内网地址（实测西北农林科技大学门户在 172.26.0.0/16 段），
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


def _positive_int(value: object) -> int:
    """把门户回的设备数解析成正整数；解析不了或非正数一律 0。

    门户这个字段有时是 int，有时是字符串 "0"。空串、None、
    "0"、0 都要归成 0，否则真值判断会把「无会话」当成「有会话」。
    """
    try:
        return max(0, int(str(value).strip() or "0"))
    except (TypeError, ValueError):
        return 0


def auth_state(config: SrunConfig) -> tuple[bool | None, str]:
    """查门户的在线状态，返回 (是否在线, 说明)。

    这个接口免凭据：`/cgi-bin/rad_user_info` 只回当前 IP 的会话信息，
    不传账号也不传密码。Portal.js 登录成功后轮询它来判断跳成功页，
    参数上只带 callback —— 多传 username 反而回 some_parameter_error。

    返回 None 表示问不出来（网络不通、门户不认这个接口、响应异常）。
    这种情况必须让调用方回退到别的探活方式，不能当成「掉线」，
    否则门户版本一变就会无意义地反复登录。
    """
    referer = f"{config.base_url.rstrip('/')}/srun_portal_pc?ac_id={config.ac_id}&theme=pro"
    ts = int(time.time() * 1000)
    try:
        data = _get(
            _opener(config.insecure),
            f"{config.base_url.rstrip('/')}{AUTH_STATE_PATH}",
            {"callback": f"jQuery{ts}", "_": str(ts)},
            referer,
            config.timeout,
        )
    except SrunError as exc:
        return None, str(exc)

    res = str(data.get("res", "")).lower()
    error = str(data.get("error", "")).lower()
    # 响应里连 res/error 都没有：多半是 CDN/WAF 挡了或返回了别的东西，
    # 不能当成「门户说没有会话」——那会导致门户版本一变就无限重登
    if not res and not error:
        return None, "门户响应无法判读（无 res/error 字段）"
    if error and error != "ok":
        # some_parameter_error 之类：接口存在但参数不合，多半是版本差异
        return None, f"门户未接受查询（{error or res}）"
    # 带 user_name / online_device_total 才说明真的有会话。
    #
    # 这两个字段不能直接用真值判断：实测无会话时门户回
    # {"user_name": "", "online_device_total": "0"}，
    # 而 "0" 是非空字符串，Python 判为真，会把「没有会话」当成「已认证」——
    # 正好和事实相反，永远不会触发重登。
    name = str(data.get("user_name") or "").strip()
    total = _positive_int(data.get("online_device_total"))
    if name or total:
        if name:
            return True, f"门户已认证（{name}）"
        return True, f"门户已认证（{data.get('online_ip')}，{total} 台设备）"
    # 实测无会话时门户回的是 {"error":"ok","online_ip":"..."}，没有 res 字段。
    # 所以这里必须认 error=="ok"，收紧成 res=="ok" 会把真实无会话误判成 unknown。
    if res == "ok" or error == "ok":
        return False, "门户在线表中无本机会话"
    return None, f"门户响应无法判读（{res or error}）"


def is_srun_portal(url: str) -> bool:
    """地址看起来像 SRUN 门户。"""
    low = (url or "").lower()
    return "srun_portal" in low or "get_challenge" in low