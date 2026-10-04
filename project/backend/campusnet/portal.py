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
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from typing import Callable

from . import charset as charset_mod
from . import probe as probe_mod
from .paths import DEFAULT_USER_AGENT
from .storage import Account

LOGIN_STATES = ("success", "already_online", "failed", "error", "unknown")

CREDENTIAL_HINTS = ("user", "name", "account", "login", "dddd", "domain", "operator")
PASSWORD_HINTS = ("pass", "pwd", "password", "upass", "0mkkey", "upwd")

# 已认证会话的标志：命中即说明这台机器本来就在线，不需要再登录。
# srun_portal_success 是 H3C 新版门户的登录成功页（西北农林科大在用）。
# 门户按 IP 判定会话，用全新无 cookie 的浏览器打开也会直接落这里。
ALREADY_AUTH_URL_MARKERS = (
    "srun_portal_success",
    "portal_success",
    "success.html",
    "/success",
)

# 登录表单「渲染完成」的判据：必须出现密码框。
#
# 不能拿 #username 当判据 —— 西农大门户的已认证成功页上有个 id="username"
# 的非 input 元素（显示当前账号），只等它出现会把成功页误判成登录页，
# 然后拿着不存在的表单去填字段，报出一堆无意义的超时。
# 密码框是所有门户登录页的共同特征，成功页上不会有。
LOGIN_FORM_READY_SELECTOR = '#password, input[type="password"]'


def looks_already_authenticated(url: str) -> bool:
    """URL 是否指向门户的登录成功页。"""
    low = (url or "").lower()
    return any(marker in low for marker in ALREADY_AUTH_URL_MARKERS)


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

# 进度回调：(阶段名, 人类可读信息)。阶段名用于前端配色与图标。
ProgressCallback = Callable[[str, str], None]


def _noop(_stage: str, _message: str) -> None:
    """默认进度回调：什么都不做，保证 on_progress 可选。"""
    return


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


class _NoProxy(urllib.request.ProxyHandler):
    """强制直连，不读 http_proxy 环境变量。

    门户地址是内网 IP（西农大 172.26.8.11），系统代理根本到不了。
    同类项目的通行做法：guet-drcom 加 `--noproxy '*'`，
    zju-web-login 用 `session.trust_env = False`。
    """

    def __init__(self) -> None:
        super().__init__({})


def _opener(cookies: CookieJar, insecure: bool = False) -> urllib.request.OpenerDirector:
    import ssl

    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        _NoProxy(),
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
    try:
        with opener.open(request, timeout=timeout) as resp:
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            raw = resp.read(200_000)
            return resp.status, charset_mod.decode(raw, content_type=hdrs.get("content-type")), resp.geturl(), hdrs
    except urllib.error.HTTPError as exc:
        hdrs = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        try:
            raw = exc.read(200_000)
        except Exception:  # noqa: BLE001
            raw = b""
        return exc.code, charset_mod.decode(raw, content_type=hdrs.get("content-type")), url, hdrs
    except urllib.error.URLError as exc:
        # 门户不可达（超时/拒绝连接/DNS 失败）必须转成状态，不能往上抛，
        # 否则一轮 check_all 抛异常会打断整个监控线程。
        return 0, "", url, {"error": str(getattr(exc, "reason", exc))}
    except (TimeoutError, OSError) as exc:
        return 0, "", url, {"error": f"{type(exc).__name__}: {exc}"}


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


def http_login(
    account: Account,
    timeout: float = 10.0,
    insecure: bool = False,
    on_progress: ProgressCallback | None = None,
) -> LoginResult:
    """按账号配置执行一次 HTTP 门户登录。

    on_progress(stage, message) 会在每个关键阶段回调一次，
    让调用方能实时把进度吐给界面，而不是干等十几秒。
    """
    emit = on_progress or _noop
    portal = account.portal_url.strip()
    if not portal:
        return LoginResult("error", "未配置门户地址", "")

    cookies = CookieJar()
    opener = _opener(cookies, insecure=insecure)
    insecure_scheme = portal.lower().startswith("https")

    emit("connect", f"正在连接认证门户 {portal}")
    status, page, final_url, hdrs = _fetch(
        _opener(cookies, insecure=insecure_scheme), portal, referer="", timeout=timeout
    )
    if status == 0:
        # 连接层失败：门户地址不对、网段没通、或服务没起
        reason = hdrs.get("error", "未知原因")
        return LoginResult("error", f"连不上门户（{reason}）", f"url={final_url}")
    if status >= 400:
        return LoginResult("error", f"打开门户失败 HTTP {status}", f"url={final_url}")

    emit("parse", f"门户已响应（HTTP {status}），正在识别登录表单")
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

    hidden = [k for k in payload if k not in (user_field, pass_field)]
    emit(
        "submit",
        f"以 {method} 提交账号 {account.username}，"
        f"账号框={user_field} 密码框={pass_field}"
        + (f"，附加字段 {','.join(hidden)}" if hidden else ""),
    )
    st, body, res_url, _ = _fetch(
        _opener(cookies, insecure=insecure_scheme), url, data=data, referer=final_url, timeout=timeout
    )
    result = classify_response(body, st, res_url)
    if not result.ok:
        emit("done", f"门户拒绝登录：{result.message}")
        return result

    emit("verify", "门户已受理，正在复检网络是否放行")
    check = probe_mod.probe(account.probe_url, timeout=timeout, insecure=insecure)
    if check.online:
        emit("done", "登录成功，网络已放行")
        return LoginResult("success", "登录成功且网络已放行", check.detail)
    if result.state == "success":
        emit("done", "门户返回成功，但复检仍未放行")
        return LoginResult("success", "门户返回成功，但探活未放行（可能需手动确认）", check.detail)
    emit("done", f"登录未成功：{result.message}")
    return result


# ---------------------------------------------------------------- 浏览器登录


def browser_login(
    account: Account,
    headless: bool = True,
    timeout: float = 30.0,
    on_progress: ProgressCallback | None = None,
) -> LoginResult:
    """用 Playwright 驱动真实浏览器完成登录，覆盖 JS / 验证码场景。"""
    emit = on_progress or _noop

    # 先校验自己的配置，再查依赖：没配门户地址时让用户去装 playwright
    # 是白折腾，改配置比重装依赖常见得多。
    portal = account.portal_url.strip()
    if not portal:
        return LoginResult("error", "未配置门户地址", "")

    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return LoginResult(
            "error",
            "未安装 playwright，无法使用浏览器登录；请 pip install playwright && playwright install chromium",
            "",
        )

    user_field = account.username_field
    pass_field = account.password_field

    emit("browser", f"正在启动浏览器（{'无头' if headless else '有头'}模式）")
    with sync_playwright() as pw:
        # 优先用系统已装的 Chrome/Edge：Playwright 自带的 Chromium 在校园网下常被
        # CDN 重置下载失败，而 channel 参数直接复用本机浏览器，不需要额外下载。
        try:
            browser = pw.chromium.launch(channel="chrome", headless=headless)
        except Exception:  # noqa: BLE001 - 没有 Chrome 就退回内置 Chromium
            emit("browser", "未找到系统 Chrome，改用 Playwright 内置 Chromium")
            browser = pw.chromium.launch(headless=headless)
        try:
            ctx = browser.new_context(user_agent=DEFAULT_USER_AGENT)
            page = ctx.new_page()
            emit("connect", f"正在打开门户 {portal}")
            page.goto(
                portal,
                wait_until="domcontentloaded",
                timeout=timeout * 1000,
            )

            # 登录区由 JS 渲染，domcontentloaded 时还没出现。
            # 先确认是不是已经认证过了——按 IP 判定的门户会直接跳成功页。
            if not _wait_for_login_form(page, timeout=timeout):
                if looks_already_authenticated(page.url):
                    final_url = page.url
                    ctx.close()
                    emit("done", "门户显示本机已认证，无需重复登录")
                    return LoginResult(
                        "already_online",
                        "门户按 IP 判定会话，这台机器已认证，无需重复登录",
                        f"url={final_url}",
                    )
                return LoginResult(
                    "error",
                    "登录表单没有渲染出来，门户可能改了页面结构",
                    f"url={page.url}",
                )

            selectors = _browser_selectors(page, user_field, pass_field)
            # 密码必须走 password_encoded：门户提交前要的是编码后的值。
            # 填明文会让 base64/md5 模式的门户一律报密码错误，
            # 而 Dr.COM 这类门户正是 base64，也是本项目的主目标场景。
            for selector, value in (
                (selectors.get("user"), account.username),
                (selectors.get("pass"), account.password_encoded),
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

            click_target = selectors.get("submit") or (
                'input[type="submit"], button[type="submit"], button'
            )
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
    """把字段名映射成 CSS 选择器，并准备常见兜底。

    密码框要特殊处理：`PASSWORD_HINTS` 永远能猜出一个名字，
    所以 `pwd or 兜底` 里的兜底是死代码。
    改成用逗号把两者串成一个选择器，
    让浏览器按顺序尝试，字段名猜错时仍能落到 type=password 上。

    提交按钮同理：H3C 新版门户（srun_portal_pc，西农大在用）
    用的是 `<button type="button" id="login-account">`，
    既不是 submit 类型也没有 login-btn 类，
    只能靠链尾的裸 button 撞上——顺序一变就点错按钮。
    把 id 显式放到链首，别指望撞运气。
    """
    out: dict[str, str] = {}
    user = _selector_for(page, user_field, CREDENTIAL_HINTS)
    pwd = _selector_for(page, pass_field, PASSWORD_HINTS)
    # 账号框启发式全落空时兜底到页面上第一个可见文本框，
    # 否则 Dr.COM 这类字段名毫无规律的门户在浏览器模式下必然失败
    if not user:
        user = _fallback_text_selector(page)
    out["user"] = user
    out["pass"] = f'{pwd}, input[type="password"]' if pwd else 'input[type="password"]'
    out["submit"] = (
        "#login-account, input[type=submit], button[type=submit], "
        "#loginSubmitBtn, .login-btn, button"
    )
    return out


def _wait_for_login_form(page, timeout: float = 30.0) -> bool:  # noqa: ANN001
    """等登录表单渲染出来。

    门户登录区由 JS 动态插入，domcontentloaded 时 DOM 里一个 input 都没有
    （实测西农大 srun_portal_pc 就是这样）。直接往下填字段会全部落空。

    分两阶段轮询：先等密码框出现（表单渲染完成的标志），
    再回查一次已认证 URL —— 有些门户按 IP 判定会话，
    会直接跳成功页，那边永远等不到密码框。
    """
    deadline = time.monotonic() + min(timeout, 20.0)
    interval = 0.4
    while time.monotonic() < deadline:
        try:
            if page.locator(LOGIN_FORM_READY_SELECTOR).count() > 0:
                return True
        except Exception:  # noqa: BLE001 - 页面正在跳转时定位器会抛
            pass
        if looks_already_authenticated(page.url):
            return False
        try:
            page.wait_for_timeout(int(interval * 1000))
        except Exception:  # noqa: BLE001
            pass
        interval = min(interval * 1.4, 1.5)
    return False


def _selector_for(page, field_name: str, hints: tuple[str, ...]) -> str:  # noqa: ANN001
    """在真实 DOM 上逐个试探候选选择器，返回第一个真能命中的。

    不能按顺序盲取第一个：`_candidate_selectors` 把 name 放最前，
    但西农大门户的输入框只有 id、没有 name，
    `input[name="username"]` 在页面上永远匹配不到。
    """
    if field_name:
        hit = _first_existing(page, _candidate_selectors(field_name))
        if hit:
            return hit
    for hint in hints:
        hit = _first_existing(page, _candidate_selectors(hint))
        if hit:
            return hit
    return ""


def _first_existing(page, selectors: list[str]) -> str:  # noqa: ANN001
    """返回页面上真实存在的第一个**可填写**选择器。

    判据不能只是 count()>0：`[name*=user]` 这类模糊匹配会命中
    user_ip / user_mac 这类 hidden 域——它们存在但填不进字。
    Dr.COM 门户的账号框叫 DDDDD，启发式猜不到 user，
    只能靠模糊匹配撞，结果撞进隐藏域，报出无意义的点击超时。
    """
    for selector in selectors:
        if _is_fillable(page, selector):
            return selector
    return ""


# 可填写的可见文本输入框：排除 hidden / readonly / disabled
FILLABLE_TEXT_FALLBACK = (
    'input[type="text"]:not([type="hidden"]):not([readonly]):not([disabled])'
)


def _is_fillable(page, selector: str) -> bool:  # noqa: ANN001
    """选择器是否指向至少一个能看见也能改的输入框。"""
    try:
        locator = page.locator(selector)
        count = locator.count()
    except Exception:  # noqa: BLE001 - 选择器非法
        return False
    for index in range(min(count, 4)):
        try:
            el = locator.nth(index)
            if el.is_visible() and el.is_editable():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _fallback_text_selector(page) -> str:  # noqa: ANN001
    """启发式全落空时的兜底：页面上第一个可见可编辑文本框。

    没有这一步，Dr.COM 这类字段名毫无规律的门户在浏览器模式下
    必然失败——账号框叫 DDDDD，既不叫 user 也不叫 name。
    """
    for selector in (FILLABLE_TEXT_FALLBACK, "input:not([type=hidden]):not([type=password])"):
        try:
            locator = page.locator(selector)
            total = locator.count()
        except Exception:  # noqa: BLE001
            continue
        for index in range(total):
            try:
                el = locator.nth(index)
                if el.is_visible() and el.is_editable():
                    return f"{selector} >> nth={index}"
            except Exception:  # noqa: BLE001
                continue
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


def login(
    account: Account,
    headless: bool = True,
    on_progress: ProgressCallback | None = None,
) -> LoginResult:
    """按账号配置选择登录路径，并全程回调进度。"""
    mode = (account.login_mode or "http").lower()
    if mode == "browser":
        return browser_login(account, headless=headless, on_progress=on_progress)
    if mode == "srun":
        return srun_login(account, on_progress=on_progress)
    if mode == "manual":
        check = probe_mod.probe(account.probe_url)
        if check.online:
            return LoginResult("already_online", "网络已放行，无需登录", check.detail)
        return LoginResult("error", "当前为人工模式：请在浏览器里手动完成认证", check.detail)
    return http_login(account, on_progress=on_progress)


def srun_login(account: Account, on_progress: ProgressCallback | None = None) -> LoginResult:
    """深澜 SRUN 认证：challenge + HMAC-MD5 + xxtea。

    与 http_login 的区别是没有任何 HTML 表单——登录全靠三段密文，
    页面里的 `<form>` 数量为 0，靠解析表单必然失败。
    """
    emit = on_progress or _noop
    from . import srun as srun_mod

    base = account.portal_url.strip()
    if not base:
        return LoginResult("error", "未配置门户地址", "")

    ip = account.extra_fields.get("ip") or account.extra_fields.get("user_ip") or ""
    if not ip:
        ip = _local_ip_for_portal(base)
    if not ip:
        return LoginResult(
            "error",
            "无法确定本机 IPv4，SRUN 认证必须带上 ip 参数",
            "可在附加字段里手动填 {\"ip\": \"10.x.x.x\"}",
        )

    config = srun_mod.SrunConfig(
        base_url=base,
        username=account.username,
        password=account.password,
        ip=ip,
        ac_id=account.extra_fields.get("ac_id", srun_mod.DEFAULT_AC_ID),
        timeout=10.0,
    )

    try:
        ok, reason = srun_mod.srun_login(config, on_progress=on_progress)
    except srun_mod.SrunError as exc:
        emit("fail", str(exc))
        return LoginResult("error", str(exc), "")

    if not ok:
        return LoginResult("failed", reason, f"ip={ip}")

    check = probe_mod.probe(account.probe_url)
    if check.online:
        emit("done", "登录成功，网络已放行")
        return LoginResult("success", "登录成功且网络已放行", check.detail)
    emit("warn", "门户已受理，但复检仍未放行")
    return LoginResult("success", f"门户返回成功（{reason}），但复检未放行", check.detail)


def _local_ip_for_portal(portal_url: str) -> str:
    """用一次 UDP connect 猜本机出口 IP。

    不建立真实连接，不发包，只是让内核按路由表选源地址。
    目的地址用门户 IP，保证路由选择与实际访问门户一致。
    """
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(portal_url)
    host = parts.hostname
    if not host:
        return ""
    if parts.scheme not in ("http", "https"):
        return ""

    probe_host = "10.255.255.255" if host.count(".") == 3 else "8.8.8.8"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.5)
        sock.connect((probe_host, 1))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()