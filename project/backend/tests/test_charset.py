"""字符集嗅探单元测试：GBK/GB2312 中文门户不能被解成乱码。"""

from __future__ import annotations

import pytest

from campusnet import charset

GBK_PAGE = (
    '<html><head><meta charset="gb2312"><title>Dr.COM认证系统</title></head>'
    "<body>用户名或密码错误</body></html>"
).encode("gbk")

GBK_NO_DECL = "<html><body>密码错误</body></html>".encode("gbk")
UTF8_PAGE = "<html><body>您已在线，请勿重复登录</body></html>".encode("utf-8")


# ---------- 头解析 ----------


@pytest.mark.parametrize(
    "header,expected",
    [
        ("text/html; charset=gb2312", "gbk"),
        ("text/html; charset=GBK", "gbk"),
        ('text/html; charset="utf-8"', "utf-8"),
        ("text/html", ""),
        (None, ""),
        ("", ""),
    ],
)
def test_from_header(header: str | None, expected: str) -> None:
    assert charset.from_header(header) == expected


def test_normalize_aliases() -> None:
    assert charset.normalize("GB2312") == "gbk"
    assert charset.normalize("utf8") == "utf-8"
    assert charset.normalize("windows-1252") == "latin-1"
    assert charset.normalize(None) == ""


# ---------- meta 解析 ----------


def test_from_meta_direct_charset() -> None:
    assert charset.from_meta(b'<meta charset="gb2312">') == "gbk"


def test_from_meta_http_equiv() -> None:
    raw = b'<meta http-equiv="Content-Type" content="text/html; charset=gb2312">'
    assert charset.from_meta(raw) == "gbk"


def test_from_meta_absent() -> None:
    assert charset.from_meta(b"<html><body>no meta</body></html>") == ""


# ---------- 嗅探与解码 ----------


def test_sniff_prefers_meta_over_guessing() -> None:
    assert charset.sniff(GBK_PAGE) == "gbk"


def test_sniff_utf8_page() -> None:
    assert charset.sniff(UTF8_PAGE) == "utf-8"


def test_sniff_without_declaration() -> None:
    """没写 charset 时靠试解码兜住 GBK 内容。"""
    assert charset.sniff(GBK_NO_DECL) in ("gb18030", "gbk")


def test_sniff_honours_explicit_declared() -> None:
    assert charset.sniff(GBK_NO_DECL, declared="gbk") == "gbk"


def test_decode_gbk_page_keeps_chinese() -> None:
    text = charset.decode(GBK_PAGE)
    assert "密码错误" in text or "用户名或密码错误" in text
    assert "认证系统" in text


def test_decode_utf8_page_keeps_chinese() -> None:
    text = charset.decode(UTF8_PAGE)
    assert "已在线" in text


def test_decode_no_replacement_chars_for_gbk() -> None:
    """这是修复的核心目标：GBK 页面不该出现替换字符。"""
    assert charset.needs_charset_fix(charset.decode(GBK_PAGE)) is False


def test_decode_with_content_type_hint() -> None:
    assert "密码错误" in charset.decode(GBK_NO_DECL, content_type="text/html; charset=gb2312")


def test_decode_empty_returns_empty() -> None:
    assert charset.decode(b"") == ""


def test_decode_binary_never_raises() -> None:
    assert isinstance(charset.decode(b"\xff\xfe\x00\x01\x99"), str)


def test_decode_unknown_declared_falls_back() -> None:
    assert isinstance(charset.decode(UTF8_PAGE, content_type="text/html; charset=bogus-9"), str)


def test_needs_charset_fix_detects_garbage() -> None:
    assert charset.needs_charset_fix("abc\ufffd\ufffd\ufffd") is True
    assert charset.needs_charset_fix("正常中文") is False
    assert charset.needs_charset_fix("") is False
    assert charset.needs_charset_fix("a\ufffdb") is False  # 单个替换字符不算


def test_needs_charset_fix_catches_utf8_decoded_gbk() -> None:
    """用旧的 UTF-8 硬解方式解码 GBK 页面，替换字符会大量出现。"""
    broken = GBK_PAGE.decode("utf-8", errors="replace")
    assert charset.needs_charset_fix(broken) is True
    assert charset.needs_charset_fix(charset.decode(GBK_PAGE)) is False


def test_decode_is_idempotent_for_correct_encoding() -> None:
    once = charset.decode(GBK_PAGE)
    assert "密码" in once