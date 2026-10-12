"""门户 fixture 的脱敏与离线回放。

目的：让「新增一所学校」变成用户能参与的活。用户在未认证状态下跑一条命令，
导出一份已脱敏的门户页（删掉所有输入值、脚本正文、学号、手机号、内网地址与
URL 里的令牌），贴到 issue / PR 里；我们拿它做离线回放测试，不必远程调试。

三条约束：

1. 只保留结构：标签名、name / id / type / placeholder / action / method、隐藏域名单；
2. 一切「值」都不带走：value 属性、脚本与样式正文、注释、URL 查询串；
3. 回放不联网：直接对本地 HTML 做识别，可在 CI 里跑。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

_ACCOUNT_RE = re.compile(r"(?<!\d)20\d{8}(?!\d)")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_PRIVATE_IP_RE = re.compile(
    r"(?<!\d)(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}"
    r"|192\.168\.\d{1,3})\.\d{1,3}(?!\d)"
)
_TOKEN_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")

_DROP_CONTENT_TAGS = {"script", "style", "noscript"}
_KEEP_ATTRS = {
    "name",
    "id",
    "type",
    "placeholder",
    "action",
    "method",
    "class",
    "title",
}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "track",
    "wbr",
}


@dataclass
class Fixture:
    html: str
    url: str = ""
    note: str = ""


class _Scrubber(HTMLParser):
    """重建 HTML：只留结构，丢掉一切值。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_CONTENT_TAGS:
            self._skip_depth += 1
            self.parts.append(f"<{tag}>")
            return
        kept = []
        for name, value in attrs:
            if name not in _KEEP_ATTRS:
                continue
            kept.append(f'{name}="{_scrub_text(value or "")}"')
        attributes = (" " + " ".join(kept)) if kept else ""
        self.parts.append(f"<{tag}{attributes}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_CONTENT_TAGS and self._skip_depth:
            self._skip_depth -= 1
            self.parts.append(f"</{tag}>")
            return
        if tag not in _VOID_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = _scrub_text(data).strip()
        if text:
            self.parts.append(text)

    def handle_comment(self, data: str) -> None:
        return


def _strip_query(url: str) -> str:
    return (url or "").split("?", 1)[0]


def _scrub_text(text: str) -> str:
    text = _strip_query(text)
    text = _ACCOUNT_RE.sub("<学号>", text)
    text = _PHONE_RE.sub("<手机号>", text)
    text = _PRIVATE_IP_RE.sub("<内网地址>", text)
    return _TOKEN_RE.sub("<令牌>", text)


def sanitize_html(html: str) -> str:
    """把门户页脱敏成只剩结构的 HTML。"""
    scrubber = _Scrubber()
    scrubber.feed(html or "")
    scrubber.close()
    return "".join(scrubber.parts)


def render(fixture: Fixture) -> str:
    header = [
        "<!-- 校园网自连门户 fixture（已脱敏）",
        f"     来源: {_scrub_text(fixture.url) or '(未提供)'}",
        "     已删除: 输入值、脚本与样式正文、注释、URL 查询串、学号、手机号、内网地址、长令牌",
    ]
    if fixture.note:
        header.append(f"     说明: {_scrub_text(fixture.note)}")
    header.append("     用途: 离线回放识别厂商与字段；可直接贴到 issue / PR -->")
    return "\n".join(header) + "\n" + fixture.html + "\n"


def write_fixture(fixture: Fixture, path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(fixture), encoding="utf-8")
    return target


def read_fixture(path: Path | str) -> Fixture:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    url = ""
    for line in text.splitlines()[:6]:
        match = re.match(r"\s*来源:\s*(\S+)", line)
        if match and match.group(1) != "(未提供)":
            url = match.group(1)
            break
    body = text.split("-->", 1)[1] if "-->" in text else text
    return Fixture(html=body.strip(), url=url)


def fetch_page(url: str, timeout: float = 8.0) -> str:
    """抓取门户页正文（默认按 GBK/GB2312 嗅探解码，https 门户多为自签证书）。"""
    import ssl
    import urllib.request

    from . import charset as charset_mod
    from .paths import DEFAULT_USER_AGENT

    request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    context = (
        ssl._create_unverified_context()  # noqa: S323 - 校园门户普遍自签
        if url.lower().startswith("https")
        else None
    )
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        raw = response.read()
        content_type = response.headers.get("Content-Type", "")
    return charset_mod.decode(raw, content_type=content_type)


def replay(path: Path | str) -> dict[str, object]:
    """离线回放：对本地 fixture 做厂商识别与字段提取，不联网。"""
    from . import discovery

    fixture = read_fixture(path)
    candidate = discovery.analyze_portal(fixture.url, fixture.html)
    vendor, signature = discovery.identify_vendor(fixture.html, fixture.url)
    return {
        "url": fixture.url,
        "vendor": vendor,
        "signature": signature,
        "username_field": candidate.username_field,
        "password_field": candidate.password_field,
        "suggested_login_mode": candidate.suggested_login_mode,
        "suggested_password_mode": candidate.suggested_password_mode,
        "needs_captcha": candidate.needs_captcha,
        "hidden_fields": sorted(candidate.hidden_fields),
    }
