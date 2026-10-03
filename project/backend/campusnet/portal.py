"""校园网门户登录：HTML 表单解析 + HTTP 会话登录 + Playwright 浏览器登录。

三条登录路径：
1. http    —— 纯 HTTP 会话（带 CookieJar），覆盖绝大多数 Dr.COM / H3C / 新网门户
2. browser —— Playwright 真浏览器执行 JS，适配验证码、CAS 单点登录
3. manual  —— 只探活不登录，交给用户手动点
"""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar

from . import probe as probe_mod
from .paths import DEFAULT_USER_AGENT
from .storage import Account

LOGIN_STATES = ("success", "already_online", "failed", "error", "unknown")

CREDENTIAL_HINTS = ("user", "name", "account", "login", "dddd", "domain", "operator")
PASSWORD_HINTS = ("pass", "pwd", "password", "upass", "0mkkey", "upwd")


# ---------------------------------------------------------------- 表单解析


@dataclass
class FormField:
    name: str
    value: str = ""
    kind: str = "text"

    def as_dict(self) -> dict[str, str]:
        return {self.name: self.value}


@dataclass
class LoginForm:
    action: str
    method: str = "POST"
    fields: dict[str, str] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)
    username_field: str = ""
    password_field: str = ""
    submit_field: str = ""

    def classify(self) -> "LoginForm":
        """没显式配置账号/密码字段名时，按字段名启发式猜测。"""
        if not self.username_field:
            for key, value in self.fields.items():
                low = key.lower()
                if any(h in low for h in CREDENTIAL_HINTS) and "pass" not in low:
                    # 账号输入框通常是空的；user_ip 这类带默认值的隐藏字段先跳过
                    if value:
                        continue
                    self.username_field = key
                    break
        if not self.password_field:
            for key, kind in self.types.items():
                if kind == "password":
                    self.password_field = key
                    break
        if not self.password_field:
            for key in self.fields:
                low = key.lower()
                if any(h in low for h in PASSWORD_HINTS):
                    self.password_field = key
                    break
        return self


_FORM_TAG_RE = re.compile(r"<form\b[^>]*>", re.I | re.S)
_FORM_END_RE = re.compile(r"</form\s*>", re.I)
_ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_INPUT_RE = re.compile(
    r"""<input\b[^>]*>|<button\b[^>]*>.*?</button>|<textarea\b[^>]*>.*?</textarea>""",
    re.I | re.S,
)



def _attrs(tag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _ATTR_RE.finditer(tag):
        name = match.group(1).lower()
        value = match.group(2) or match.group(3) or match.group(4) or ""
        out[name] = html.unescape(value)
    return out


def parse_forms(page_html: str) -> list[LoginForm]:
    """抽取页面里所有表单的 action/method/字段。

    只用标准库，不引三方解析器：按 <form> 出现位置切片，
    遇到对应 </form> 或下一个 <form> 收尾，容忍嵌套与畸形结构。
    """
    forms: list[LoginForm] = []
    for open_match in _FORM_TAG_RE.finditer(page_html):
        end_match = _FORM_END_RE.search(page_html, open_match.end())
        chunk = page_html[open_match.start() : end_match.start() if end_match else len(page_html)]

        form = LoginForm(action="", method="GET")
        attrs = _attrs(open_match.group(0))
        form.action = attrs.get("action", "")
        form.method = (attrs.get("method", "GET") or "GET").upper()

        for raw in _INPUT_RE.finditer(chunk):
            token = raw.group(0)
            tag_attrs = _attrs(token.split(">", 1)[0] + ">")
            name = tag_attrs.get("name", "").strip()
            if not name:
                continue
            kind = (tag_attrs.get("type") or "").lower()
            if not kind and token.lower().startswith("<button"):
                kind = "button"
            if kind in ("submit", "image", "reset", "button", "file"):
                if kind == "submit" and not form.submit_field:
                    form.submit_field = name
                continue
            value = tag_attrs.get("value", "")
            if not value and token.lower().startswith("<textarea"):
                value = re.sub(r"<[^>]+>", "", token)
            form.fields[name] = html.unescape(value)
            form.types[name] = kind or "text"
        forms.append(form)
    return forms


def pick_login_form(forms: list[LoginForm]) -> LoginForm | None:
    """优先挑带密码框、字段最多的表单。"""
    candidates = [f for f in forms if f.fields]
    if not candidates:
        return None
    scored = sorted(
        candidates,
        key=lambda f: (
            any(h in k.lower() for k in f.fields for h in PASSWORD_HINTS),
            len(f.fields),
        ),
        reverse=True,
    )
    return scored[0].classify()


# ---------------------------------------------------------------- HTTP 登录


@dataclass
class LoginResult:
    state: str
    message: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state in ("success", "already_online")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, ANN001
        return None


def _opener(cookies: CookieJar, insecure: bool = False) -> urllib.request.OpenerDirector:
    import ssl

    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        _NoRedirect(),
        urllib.request.HTTPCookieProcessor(cookies),
        urllib.request.HTTPSHandler(context=ctx),
    )
    opener.addheaders = [("User-Agent", DEFAULT_USER_AGENT)]
    return opener


def _fetch(
    opener: urllib.request.OpenerDirector,
    url: str,
    data: bytes | None = None,
    referer: str = "",
    timeout: float = 10.0,
) -> tuple[int, str, str, dict[str, str]]:
    headers = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, data=data, headers=headers)
    method = "POST" if data is not None else "GET"
    try:
        with opener.open(request, timeout=timeout) as resp:
            body = resp.read(200_000).decode("utf-8", errors="replace")
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, body, resp.geturl(), hdrs
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(200_000).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        hdrs = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return exc.code, body, url, hdrs


def classify_response(body: str, status: int, final_url: str) -> LoginResult:
    """判断门户返回的是成功、已在线还是失败。"""
    text = html.unescape(body or "")
    low = text.lower()

    for token in ('"result":1', '"result" : 1', "result=1", '"success":true', 'success": true'):
        if token.replace(" ", "") in low.replace(" ", ""):
            return LoginResult("success", "门户返回成功", token)

    if any(k in low for k in ("already online", "已在线", "already_online", "在线", "重复登录")):
        return LoginResult("already_online", "账号已在线，无需重复登录", "")

    for token in ("passworderror", "密码错误", "密码不正确", "wrongpassword", "invalidpassword"):
        if token in low:
            return LoginResult("failed", "门户提示密码错误", token)

    for token in ('"result":0', '"success":false', "登录失败", "认证失败", "userdeny"):
        if token.replace(" ", "") in low.replace(" ", ""):
            return LoginResult("failed", "门户返回登录失败", token)

    if probe_mod.is_portal_url(final_url) and status in (200, 302, 401, 403):
        return LoginResult("failed", "提交后仍停留在登录页", f"status={status} url={final_url}")

    if status in (200, 302, 303):
        return LoginResult("success", "未匹配到明确失败标记，按成功处理", f"status={status}")

    return LoginResult("error", f"门户返回异常状态 {status}", f"status={status} url={final_url}")


def http_login(account: Account, timeout: float = 10.0, insecure: bool = False) -> LoginResult:
    """按账号配置执行一次 HTTP 门户登录。"""
    portal = account.portal_url.strip()
    if not portal:
        return LoginResult("error", "未配置门户地址", "")

    cookies = CookieJar()
    opener = _opener(cookies, insecure=insecure)
    insecure_scheme = portal.lower().startswith("https")

    status, page, final_url, _ = _fetch(
        _opener(cookies, insecure=insecure_scheme), portal, referer="", timeout=timeout
    )
    if status >= 400:
        return LoginResult("error", f"打开门户失败 HTTP {status}", f"url={final_url}")

    forms = parse_forms(page)
    form = pick_login_form(forms)
    if form is None:
        return LoginResult(
            "error",
            "门户页面里没找到登录表单，可能需要浏览器模式或人工验证码",
            f"forms={len(forms)}",
        )

    user_field = account.username_field or form.username_field
    pass_field = account.password_field or form.password_field
    if not user_field or not pass_field:
        return LoginResult(
            "error",
            f"未能识别账号/密码字段（账号={user_field or '?'} 密码={pass_field or '?'}），请在配置里手动指定",
            ",".join(form.fields),
        )

    payload = dict(form.fields)
    for key, value in account.extra_fields.items():
        payload[key] = value
    payload[user_field] = account.username
    payload[pass_field] = account.password_encoded

    action = form.action or final_url
    target = urllib.parse.urljoin(final_url, action)

    data = None
    method = form.method
    if method == "POST":
        data = urllib.parse.urlencode(payload).encode("utf-8")
        url = target
    else:
        url = f"{target}?{urllib.parse.urlencode(payload)}"
        method = "GET"

    st, body, res_url, _ = _fetch(
        _opener(cookies, insecure=insecure_scheme), url, data=data, referer=final_url, timeout=timeout
    )
    result = classify_response(body, st, res_url)

    # 门户登录成功与否，最终以探活结果为准
    if result.ok:
        check = probe_mod.probe(account.probe_url, timeout=timeout, insecure=insecure)
        if check.online:
            return LoginResult("success", "登录成功且网络已放行", check.detail)
        if result.state == "success":
            return LoginResult("success", "门户返回成功，但探活未放行（可能需手动确认）", check.detail)
    return result


# ---------------------------------------------------------------- 浏览器登录


def browser_login(account: Account, headless: bool = True, timeout: float = 30.0) -> LoginResult:
    """用 Playwright 驱动真实浏览器完成登录，覆盖 JS / 验证码场景。"""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return LoginResult(
            "error",
            "未安装 playwright，无法使用浏览器登录；请 pip install playwright && playwright install chromium",
            "",
        )

    portal = account.portal_url.strip()
    if not portal:
        return LoginResult("error", "未配置门户地址", "")

    user_field = account.username_field
    pass_field = account.password_field

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            ctx = browser.new_context(user_agent=DEFAULT_USER_AGENT)
            page = ctx.new_page()
            page.goto(portal, wait_until="domcontentloaded", timeout=timeout * 1000)

            selectors = _browser_selectors(page, user_field, pass_field)
            for selector, value in (
                (selectors.get("user"), account.username),
                (selectors.get("pass"), account.password),
            ):
                if not selector:
                    continue
                try:
                    page.fill(selector, value, timeout=8000)
                except Exception:  # noqa: BLE001 - 换用键盘输入兜底
                    try:
                        page.click(selector, timeout=4000)
                        page.keyboard.type(value)
                    except Exception as inner:  # noqa: BLE001
                        return LoginResult("error", f"填写字段失败: {inner}", selector)

            for key, value in account.extra_fields.items():
                for selector in _candidate_selectors(key):
                    try:
                        page.fill(selector, value, timeout=1500)
                        break
                    except Exception:  # noqa: BLE001
                        continue

            click_target = selectors.get("submit") or "input[type=submit], button[type=submit], button"
            try:
                page.click(click_target, timeout=8000)
            except Exception as exc:  # noqa: BLE001
                return LoginResult("error", f"点击登录按钮失败: {exc}", click_target)

            try:
                page.wait_for_timeout(3000)
            except Exception:  # noqa: BLE001
                pass

            content = page.content()
            final_url = page.url
            ctx.close()
        finally:
            browser.close()

    result = classify_response(content, 200, final_url)
    check = probe_mod.probe(account.probe_url, timeout=timeout / 6 or 5.0)
    if result.ok and not check.online:
        return LoginResult(result.state, f"{result.message}；但探活显示未放行", check.detail)
    if result.ok:
        return LoginResult("success", f"{result.message}；探活通过", check.detail)
    return result


def _browser_selectors(page, user_field: str, pass_field: str) -> dict[str, str]:  # noqa: ANN001
    """把字段名映射成 CSS 选择器，并准备常见兜底。"""
    out: dict[str, str] = {}
    user = _selector_for(user_field, CREDENTIAL_HINTS)
    pwd = _selector_for(pass_field, PASSWORD_HINTS)
    out["user"] = user
    out["pass"] = pwd or 'input[type="password"]'
    out["submit"] = 'input[type="submit"], button[type="submit"], #loginSubmitBtn, .login-btn, button'
    return out


def _selector_for(field_name: str, hints: tuple[str, ...]) -> str:
    if field_name:
        for selector in _candidate_selectors(field_name):
            return selector
    for hint in hints:
        for selector in _candidate_selectors(hint):
            return selector
    return ""


def _candidate_selectors(field_name: str) -> list[str]:
    safe = re.sub(r"([^\w-])", lambda m: f"\\{m.group(1)}", field_name)
    return [
        f'input[name="{field_name}"]',
        f'input[id="{field_name}"]',
        f'input#{safe}',
        f"[name*={safe}]",
        f"#{safe}",
    ]


def login(account: Account, headless: bool = True) -> LoginResult:
    mode = (account.login_mode or "http").lower()
    if mode == "browser":
        return browser_login(account, headless=headless)
    if mode == "manual":
        check = probe_mod.probe(account.probe_url)
        if check.online:
            return LoginResult("already_online", "网络已放行，无需登录", check.detail)
        return LoginResult("error", "当前为人工模式：请在浏览器里手动完成认证", check.detail)
    return http_login(account)