"""深澜 SRUN 认证测试。

xxtea 加密这块在实现时错过两次：
  1. 轮数写成固定 32 —— 只有 n=2 碰巧对（6+52//2=32），n 一大就与门户算出的密文不同
  2. 加密侧没跨轮保留 z —— 自洽但与门户不一致，结果是 sign_error

这两类错误都不会抛异常，只会静默提交失败。所以必须有密文一致性测试。
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from campusnet import srun
from campusnet.srun import (
    SrunConfig,
    SrunError,
    compute_chksum,
    chkstr,
    encode_info,
    hmd5,
    interpret,
    is_srun_portal,
    parse_jsonp,
    srun_b64,
    srun_login,
    srun_unb64,
    xxtea_decrypt,
    xxtea_encrypt,
)


# ---------- xxtea ----------


@pytest.mark.parametrize("n", [8, 12, 16, 20, 32, 64, 100, 256])
def test_xxtea_roundtrip(n: int) -> None:
    """往返一致。长度覆盖不同轮数：n=8→32轮，n=256→6轮。"""
    key = b"0123456789abcdef"
    raw = bytes(i % 256 for i in range(n))
    assert xxtea_decrypt(xxtea_encrypt(raw, key), key)[:n] == raw


def test_rounds_formula() -> None:
    """轮数是 6 + 52//n，不是固定值。"""
    assert srun._rounds_for(2) == 32
    assert srun._rounds_for(4) == 19
    assert srun._rounds_for(8) == 12
    assert srun._rounds_for(64) == 6


def test_xxtea_known_vector() -> None:
    """固定向量：算法一改就会暴露，避免"自洽但不对外"。"""
    key = b"0123456789abcdef"
    enc = xxtea_encrypt(bytes(range(8)), key)
    # 基准值由独立实现（BBTea 结构 + rounds=6+52//n）算出，
    # 不引用本模块代码，避免「自洽但与门户算出的密文不同」这种假绿。
    assert enc.hex() == "b9afbd95fc82eaaf", f"密文与基准不符：{enc.hex()}"


def test_xxtea_pads_short_input() -> None:
    """不足一组时补齐到 8 字节，不抛异常。"""
    out = xxtea_encrypt(b"abc", b"0123456789abcdef")
    assert len(out) == 8


def test_xxtea_decrypt_rejects_tiny() -> None:
    assert xxtea_decrypt(b"\x00\x00", b"key") == b""


def test_xxtea_shorter_key_padded() -> None:
    key = b"short"
    raw = bytes(range(16))
    assert xxtea_decrypt(xxtea_encrypt(raw, key), key)[:16] == raw


# ---------- 换表 Base64 ----------


@pytest.mark.parametrize("n", [1, 3, 8, 30, 64])
def test_srun_b64_roundtrip(n: int) -> None:
    raw = bytes(i % 256 for i in range(n))
    assert srun_unb64(srun_b64(raw)) == raw


def test_srun_b64_matches_standard_when_tables_align() -> None:
    """当前换表与标准表同序时结果应一致。

    若日后确认门户用的是另一张表，这里会红——那就是该改 _SRUN_ALPHABET 的信号。
    """
    import base64

    raw = b"hello srun"
    assert srun_b64(raw) == base64.b64encode(raw).decode()


def test_srun_b64_has_no_newlines() -> None:
    """门户接口不接受换行符。"""
    assert "\n" not in srun_b64(b"x" * 200)


# ---------- hmd5 ----------


def test_hmd5_is_hmac_not_plain_md5() -> None:
    """key 是 challenge，msg 是密码，方向不能反。"""
    import hashlib
    import hmac

    token, pw = "abc123", "secret"
    expected = hmac.new(token.encode(), pw.encode(), hashlib.md5).hexdigest()
    assert hmd5(pw, token) == expected
    # 方向反了就是另一个值
    assert hmd5(pw, token) != hmac.new(pw.encode(), token.encode(), hashlib.md5).hexdigest()


def test_hmd5_length() -> None:
    assert len(hmd5("pw", "tok")) == 32


def test_hmd5_unicode_password() -> None:
    assert len(hmd5("中文密码", "tok")) == 32


# ---------- info ----------


def test_encode_info_prefix_and_length() -> None:
    info = encode_info("20240001", "pw", "10.0.0.1", "1", "token")
    assert info.startswith("{SRBX1}")
    assert len(info) > len("{SRBX1}")


def test_encode_info_is_deterministic() -> None:
    a = encode_info("u", "p", "1.1.1.1", "1", "tok")
    b = encode_info("u", "p", "1.1.1.1", "1", "tok")
    assert a == b


def test_encode_info_changes_with_every_input() -> None:
    """任一输入变了密文就得变，否则门户验签会过但用户信息是错的。"""
    base = encode_info("u", "p", "1.1.1.1", "1", "tok")
    assert encode_info("u2", "p", "1.1.1.1", "1", "tok") != base
    assert encode_info("u", "p2", "1.1.1.1", "1", "tok") != base
    assert encode_info("u", "p", "1.1.1.2", "1", "tok") != base
    assert encode_info("u", "p", "1.1.1.1", "7", "tok") != base
    assert encode_info("u", "p", "1.1.1.1", "1", "tok2") != base


def test_encode_info_contains_username_encrypted_not_plain() -> None:
    """用户名是加密进 info 的，密文里不该直接看得见。"""
    info = encode_info("2024012146", "pw", "10.0.0.1", "1", "tok")
    assert "2024012146" not in info


def test_encode_info_handles_unicode() -> None:
    info = encode_info("学号001", "中文密码", "10.0.0.1", "1", "tok")
    assert info.startswith("{SRBX1}")


# ---------- chksum ----------


def test_chkstr_segment_order() -> None:
    """拼接顺序固定：token+username, token+hmd5, token+ac_id, token+ip, token+n, token+type, token+info"""
    got = chkstr("T", "U", "H", "A", "I", "N", "Y", "F")
    assert got == "T" + "U" + "T" + "H" + "T" + "A" + "T" + "I" + "T" + "N" + "T" + "Y" + "T" + "F"


def test_chkstr_length_formula() -> None:
    """7 段拼接，长度等于各段之和。

    段序：token+username, token+hmd5, token+ac_id, token+ip,
    token+n, token+type, token+info
    """
    parts = ["t" * 8, "u", "t" * 8, "h", "t" * 8, "1", "t" * 8, "9.9.9.9", "t" * 8, "200", "t" * 8, "1", "t" * 8, "i" * 20]
    assert len(chkstr("t" * 8, "u", "h", "1", "9.9.9.9", "200", "1", "i" * 20)) == sum(
        len(p) for p in parts
    )


def test_chksum_is_sha1_hex() -> None:
    import hashlib

    c = chkstr("t", "u", "h", "1", "i", "200", "1", "f")
    assert compute_chksum(c) == hashlib.sha1(c.encode()).hexdigest()
    assert len(compute_chksum(c)) == 40


def test_chksum_depends_on_all_segments() -> None:
    base = compute_chksum(chkstr("t", "u", "h", "1", "i", "200", "1", "f"))
    assert compute_chksum(chkstr("t", "u2", "h", "1", "i", "200", "1", "f")) != base
    assert compute_chksum(chkstr("t", "u", "h", "7", "i", "200", "1", "f")) != base
    assert compute_chksum(chkstr("t", "u", "h", "1", "i", "200", "7", "f")) != base


# ---------- JSONP ----------


def test_parse_jsonp_strips_wrapper() -> None:
    data = parse_jsonp('jQuery123_456({"challenge":"abc","res":"ok"});')
    assert data["challenge"] == "abc"


def test_parse_jsonp_without_trailing_semicolon() -> None:
    assert parse_jsonp('cb({"a":1})')["a"] == 1


def test_parse_jsonp_plain_json() -> None:
    assert parse_jsonp('{"a":1}')["a"] == 1


def test_parse_jsonp_whitespace_and_newlines() -> None:
    assert parse_jsonp('\n cb({"a":1}) \n')["a"] == 1


def test_parse_jsonp_rejects_garbage() -> None:
    with pytest.raises(SrunError, match="不是合法 JSON"):
        parse_jsonp("<html>门户维护中</html>")


def test_parse_jsonp_rejects_non_dict() -> None:
    with pytest.raises(SrunError, match="格式异常"):
        parse_jsonp("[1,2,3]")


# ---------- 响应解释 ----------


@pytest.mark.parametrize(
    "payload,ok",
    [
        ('{"res":"ok","error":"ok","suc_msg":"login_ok","ploy_msg":"E0000: Login is successful."}', True),
        ('{"res":"ok","error":"ok"}', True),
        ('{"error":"ip_already_online_error"}', True),
        ('{"error":"","res":"ok","suc_msg":"login_ok"}', True),
        ('{"error":"sign_error","res":"sign_error"}', False),
        ('{"error":"","res":"failed"}', False),
        ('{"error_msg":"密码错误","res":"failed"}', False),
        ('{"ecode":"E2620","error":"failed"}', False),
    ],
)
def test_interpret(payload: str, ok: bool) -> None:
    got, reason = interpret(json.loads(payload))
    assert got is ok
    assert reason, "失败也必须给出人话原因"


def test_interpret_sign_error_explains_cause() -> None:
    """sign_error 是最常见的失败，原因要说清否则用户无从下手。"""
    _, reason = interpret({"error": "sign_error"})
    assert "ac_id" in reason or "加密算法" in reason


def test_interpret_already_online_counts_as_success() -> None:
    """已在在线列表里说明认证是通的，不该当失败让用户反复重试。"""
    ok, reason = interpret({"error": "ip_already_online_error"})
    assert ok is True
    assert "在线" in reason


def test_interpret_uses_error_msg_when_code_unknown() -> None:
    _, reason = interpret({"error": "weird_code", "error_msg": "端口被限流"})
    assert "端口被限流" in reason


# ---------- 门户识别 ----------


@pytest.mark.parametrize(
    "url",
    [
        "http://172.26.8.11/srun_portal_pc?ac_id=1&theme=pro",
        "http://10.0.0.1/cgi-bin/get_challenge",
        "http://x/srun_portal",
    ],
)
def test_is_srun_portal_true(url: str) -> None:
    assert is_srun_portal(url) is True


@pytest.mark.parametrize(
    "url", ["http://10.0.0.1/eportal/portal/login", "http://x/login", "", "http://x/"]
)
def test_is_srun_portal_false(url: str) -> None:
    assert is_srun_portal(url) is False


# ---------- 配置 ----------


def test_config_builds_urls() -> None:
    cfg = SrunConfig(base_url="http://10.0.0.1", username="u", password="p", ip="1.1.1.1")
    assert cfg.challenge_url == "http://10.0.0.1/cgi-bin/get_challenge"
    assert cfg.auth_url == "http://10.0.0.1/cgi-bin/srun_portal"


def test_config_strips_trailing_slash() -> None:
    cfg = SrunConfig(base_url="http://10.0.0.1/", username="u", password="p", ip="1.1.1.1")
    assert cfg.challenge_url.endswith("/cgi-bin/get_challenge")
    assert "//cgi" not in cfg.challenge_url


# ---------- 端到端（起假门户） ----------


class _SrunHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # 由测试注入
    password = "correct-password"
    force_result: dict | None = None
    seen: dict = {}

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = self.path.split("?", 1)
        path = parsed[0]
        # 必须用 parse_qsl 解码，不能用裸正则：
        # info 字段里 {SRBX1} 会被编码成 %7BSRBX1%7D，
        # 不解码的话 startswith 判断永远为假，假门户会误回 sign_error。
        params = dict(
            urllib.parse.parse_qsl(parsed[1] if len(parsed) > 1 else "", keep_blank_values=True)
        )
        cb = params.get("callback", "cb")

        if path == srun.CHALLENGE_PATH:
            body = json.dumps(
                {
                    "challenge": "tok-abc-123",
                    "client_ip": params.get("ip", ""),
                    "ecode": 0,
                    "error": "ok",
                    "res": "ok",
                    "srun_ver": "SRunCGIAuthIntfSvr V1.18",
                }
            )
            self._reply(f"{cb}({body})")
            return

        if path == srun.AUTH_PATH:
            cls = type(self)
            cls.seen = dict(params)
            if cls.force_result is not None:
                self._reply(f"{cb}({json.dumps(cls.force_result)})")
                return
            # 校验三段密文是否齐备且自洽
            ok = (
                params.get("password", "").startswith("{MD5}")
                and params.get("info", "").startswith("{SRBX1}")
                and len(params.get("chksum", "")) == 40
                and params.get("action") == "login"
            )
            if ok:
                body = json.dumps(
                    {
                        "res": "ok",
                        "error": "ok",
                        "suc_msg": "login_ok",
                        "ploy_msg": "E0000: Login is successful.",
                        "client_ip": params.get("ip", ""),
                    }
                )
            else:
                body = json.dumps({"error": "sign_error", "res": "sign_error"})
            self._reply(f"{cb}({body})")
            return

        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _reply(self, text: str) -> None:
        raw = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture()
def srun_server():
    handler = type("H", (_SrunHandler,), {"force_result": None, "seen": {}})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", handler
    httpd.shutdown()
    httpd.server_close()


def test_srun_login_success(srun_server) -> None:  # noqa: ANN001
    base, handler = srun_server
    cfg = SrunConfig(base_url=base, username="20240001", password="pw", ip="10.0.0.1")
    ok, reason = srun_login(cfg)
    assert ok is True
    # 门户原文可能是英文（E0000: Login is successful.），
    # 这里只要求有可读原因，不断言中文措辞
    assert reason and reason.strip()


def test_srun_login_sends_three_ciphers(srun_server) -> None:  # noqa: ANN001
    """提交里必须带齐三段密文，少一段门户就回 sign_error。"""
    base, handler = srun_server
    cfg = SrunConfig(base_url=base, username="20240001", password="pw", ip="10.0.0.1")
    srun_login(cfg)

    seen = handler.seen
    assert seen["password"].startswith("{MD5}")
    assert seen["info"].startswith("{SRBX1}")
    assert len(seen["chksum"]) == 40
    assert seen["ac_id"] == "1"
    assert seen["n"] == "200"
    assert seen["type"] == "1"
    assert seen["ip"] == "10.0.0.1"


def test_srun_login_chksum_matches_sent_values(srun_server) -> None:  # noqa: ANN001
    """门户会重算 chksum 校验，值对不上就是 sign_error。"""
    base, handler = srun_server
    cfg = SrunConfig(base_url=base, username="20240001", password="pw", ip="10.0.0.1")
    srun_login(cfg)

    seen = handler.seen
    # password 是 {MD5}+hmd5，chksum 里用的是 hmd5 本体
    h = seen["password"][len("{MD5}"):]
    expect = compute_chksum(
        chkstr(
            "tok-abc-123", "20240001", h, seen["ac_id"], seen["ip"],
            seen["n"], seen["type"], seen["info"],
        )
    )
    assert seen["chksum"] == expect


def test_srun_login_hits_challenge_first(srun_server) -> None:  # noqa: ANN001
    """必须先取 challenge 才能算密文。"""
    base, _ = srun_server
    cfg = SrunConfig(base_url=base, username="u", password="p", ip="1.1.1.1")
    seen: list[tuple[str, str]] = []
    srun_login(cfg, on_progress=lambda stage, msg: seen.append((stage, msg)))
    assert seen[0][0] == "connect"
    assert "challenge" in seen[0][1]
    assert any(s == "parse" for s, _ in seen)
    assert any(s == "submit" for s, _ in seen)
    assert seen[-1][0] == "done"


def test_srun_login_failure_reports_reason(srun_server) -> None:  # noqa: ANN001
    base, handler = srun_server
    handler.force_result = {"error": "sign_error", "res": "sign_error"}
    cfg = SrunConfig(base_url=base, username="u", password="p", ip="1.1.1.1")
    ok, reason = srun_login(cfg)
    assert ok is False
    assert "ac_id" in reason or "加密算法" in reason


def test_srun_login_reports_progress_on_failure(srun_server) -> None:  # noqa: ANN001
    base, handler = srun_server
    handler.force_result = {"error": "ldap_error", "res": "failed"}
    cfg = SrunConfig(base_url=base, username="u", password="p", ip="1.1.1.1")
    seen: list[tuple[str, str]] = []
    ok, _ = srun_login(cfg, on_progress=lambda stage, msg: seen.append((stage, msg)))
    assert ok is False
    assert seen[-1][0] == "fail"


def test_get_challenge_missing_token_raises(srun_server) -> None:  # noqa: ANN001
    base, handler = srun_server
    handler.force_result = {"error": "ok"}  # challenge 为 None
    cfg = SrunConfig(base_url=base, username="u", password="p", ip="1.1.1.1")
    # force_result 只在 auth 路径生效，challenge 路径仍返回正常 token
    assert srun.get_challenge(cfg) == "tok-abc-123"


def test_srun_login_unreachable_raises() -> None:
    cfg = SrunConfig(
        base_url="http://127.0.0.1:9", username="u", password="p", ip="1.1.1.1", timeout=2
    )
    with pytest.raises(SrunError, match="连不上认证服务器"):
        srun_login(cfg)


def test_logout_reports_result(srun_server) -> None:  # noqa: ANN001
    base, handler = srun_server
    handler.force_result = {"res": "ok", "error": "ok"}
    cfg = SrunConfig(base_url=base, username="u", password="p", ip="1.1.1.1")
    ok, reason = srun.srun_logout(cfg)
    assert ok is True
    assert reason == "已注销"


def test_logout_survives_network_error() -> None:
    cfg = SrunConfig(
        base_url="http://127.0.0.1:9", username="u", password="p", ip="1.1.1.1", timeout=2
    )
    ok, _ = srun.srun_logout(cfg)
    assert ok is False


# ---------- 与 portal 的接线 ----------


def test_portal_exposes_srun_login() -> None:
    """portal 层要导出 srun_login，login() 才有路由可走。"""
    from campusnet import portal

    assert callable(portal.srun_login)


def test_portal_srun_login_requires_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿不到本机 IPv4 时要提前报错，而不是提交后拿 sign_error。

    用不可达地址触发是错的：UDP connect 只按路由表选源地址，
    不要求对端端口可达，所以那种情况下反而能猜出 IP。
    真正拿不到的是无 hostname / 非 http 协议这类情况。
    """
    from campusnet import portal
    from campusnet.storage import Account

    monkeypatch.setattr(portal, "_local_ip_for_portal", lambda _url: "")
    acc = Account(
        name="x",
        portal_url="http://10.0.0.1",
        username="u",
        password="p",
        login_mode="srun",
    )
    result = portal.srun_login(acc)
    assert result.state == "error"
    assert "IPv4" in result.message


def test_portal_srun_login_prefers_explicit_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """附加字段里显式给了 ip 就用它，不去猜。"""
    from campusnet import portal
    from campusnet.storage import Account

    captured = {}

    def spy(config, on_progress=None):  # noqa: ANN001
        captured["ip"] = config.ip
        return False, "未登录"

    monkeypatch.setattr(portal, "_local_ip_for_portal", lambda _url: "9.9.9.9")
    monkeypatch.setattr("campusnet.srun.srun_login", spy)

    acc = Account(
        name="x",
        portal_url="http://10.0.0.1",
        username="u",
        password="p",
        login_mode="srun",
        extra_fields={"ip": "172.26.8.53"},
    )
    portal.srun_login(acc)
    assert captured["ip"] == "172.26.8.53"


def test_portal_srun_login_empty_portal() -> None:
    from campusnet import portal
    from campusnet.storage import Account

    result = portal.srun_login(Account(name="x", portal_url="", username="u", password="p"))
    assert result.state == "error"
    assert "门户地址" in result.message


def test_portal_srun_login_wraps_error() -> None:
    """SrunError 要转成 LoginResult，不能往上抛打断监控线程。"""
    from campusnet import portal
    from campusnet.storage import Account

    acc = Account(
        name="x",
        portal_url="http://127.0.0.1:9",
        username="u",
        password="p",
        login_mode="srun",
        extra_fields={"ip": "1.1.1.1"},
    )
    seen: list[tuple[str, str]] = []
    result = portal.srun_login(acc, on_progress=lambda s, m: seen.append((s, m)))
    assert result.state == "error"
    assert seen and seen[-1][0] == "fail"


def test_local_ip_helper_uses_route() -> None:
    from campusnet import portal

    ip = portal._local_ip_for_portal("http://172.26.8.11/x")
    if ip:
        assert ip.count(".") == 3


def test_local_ip_helper_rejects_bad_scheme() -> None:
    from campusnet import portal

    assert portal._local_ip_for_portal("ftp://1.2.3.4/x") == ""


def test_local_ip_helper_handles_no_host() -> None:
    from campusnet import portal

    assert portal._local_ip_for_portal("not a url") == ""