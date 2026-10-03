"""字符集嗅探：校园网门户大量使用 GBK/GB2312，按 UTF-8 硬解会得到乱码字段名。

顺序：HTTP 头 charset → <meta charset> → 试解码（UTF-8 严格 → GBK → latin-1）。
"""

from __future__ import annotations

import re

_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w-]+)""", re.I
)
_META_HTTP_EQUIV_RE = re.compile(
    rb"""<meta[^>]+content\s*=\s*["'][^"']*charset\s*=\s*([\w-]+)""", re.I
)

_ALIASES = {
    "gb2312": "gbk",
    "gb_2312": "gbk",
    "gb-2312": "gbk",
    "gbk": "gbk",
    "gb18030": "gb18030",
    "utf8": "utf-8",
    "utf-8": "utf-8",
    "iso-8859-1": "latin-1",
    "windows-1252": "latin-1",
    "latin1": "latin-1",
}


def normalize(name: str | None) -> str:
    if not name:
        return ""
    return _ALIASES.get(name.strip().strip("\"'").lower(), name.strip().strip("\"'").lower())


_HEADER_CHARSET_RE = re.compile(r"""charset\s*=\s*["']?\s*([\w-]+)""", re.I)


def from_header(content_type: str | None) -> str:
    """从 Content-Type 里取 charset，容忍带引号的写法。"""
    if not content_type:
        return ""
    match = _HEADER_CHARSET_RE.search(content_type)
    return normalize(match.group(1)) if match else ""


def from_meta(head: bytes) -> str:
    """从页面 head 的 <meta> 里取 charset。"""
    for pattern in (_META_CHARSET_RE, _META_HTTP_EQUIV_RE):
        match = pattern.search(head[:4096])
        if match:
            try:
                return normalize(match.group(1).decode("ascii"))
            except UnicodeDecodeError:
                continue
    return ""


def sniff(raw: bytes, content_type: str | None = None, declared: str = "") -> str:
    """综合判定页面编码，返回可用的 codec 名。"""
    candidates: list[str] = []
    for name in (declared, from_header(content_type), from_meta(raw)):
        if name and name not in candidates:
            candidates.append(name)

    for name in candidates:
        try:
            raw.decode(name)
            return name
        except (UnicodeDecodeError, LookupError):
            continue

    # 头里没写或写错了：UTF-8 严格解，再 GB18030（超集，覆盖 GBK/GB2312）
    for name in ("utf-8", "gb18030"):
        try:
            raw.decode(name)
            return name
        except UnicodeDecodeError:
            continue
    return "latin-1"


def decode(
    raw: bytes,
    content_type: str | None = None,
    declared: str = "",
    errors: str = "replace",
) -> str:
    """按嗅探结果解码；latin-1 永不失败，作为最后兜底。"""
    if not raw:
        return ""
    name = sniff(raw, content_type=content_type, declared=declared)
    try:
        return raw.decode(name, errors=errors if name != "latin-1" else "replace")
    except (UnicodeDecodeError, LookupError):
        return raw.decode("utf-8", errors="replace")


def needs_charset_fix(text: str) -> bool:
    """粗判是否解错了：出现大量替换字符或典型乱码片段。"""
    if not text:
        return False
    return text.count("\ufffd") >= 3