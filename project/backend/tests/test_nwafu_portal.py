"""H3C srun_portal_pc（西北农林科大在用）路径的回归测试。

提交 7064638 针对真实门户改了三件事，但当时零测试覆盖：
  1. 提交按钮用 `<button type="button" id="login-account">`
  2. 登录区由 JS 渲染，domcontentloaded 时还没有表单
  3. 门户按 IP 判定会话，已认证时直接跳 srun_portal_success

这些行为只在真实门户掉线态才跑得到，用桩把时序与跳转补齐。
"""

from __future__ import annotations

import base64
import sys
import types

import pytest

from campusnet import portal
from campusnet.portal import (
    _browser_selectors,
    _first_existing,
    _is_fillable,
    _wait_for_login_form,
    browser_login,
    looks_already_authenticated,
)
from campusnet.storage import Account

NWAFU_LOGIN_URL = "http://172.26.8.11/srun_portal_pc?ac_id=1&theme=pro"
NWAFU_SUCCESS_URL = "http://172.26.8.11/srun_portal_success?ac_id=1&theme=pro"


# ---------- looks_already_authenticated：纯函数 ----------


@pytest.mark.parametrize(
    "url",
    [
        NWAFU_SUCCESS_URL,
        "http://10.0.0.1/srun_portal_success?ac_id=1",
        "HTTP://172.26.8.11/SRUN_PORTAL_SUCCESS",
        "http://x/portal_success",
        "http://x/login/success.html",
        "http://x/success",
    ],
)
def test_detects_success_page(url: str) -> None:
    assert looks_already_authenticated(url) is True


@pytest.mark.parametrize(
    "url",
    [
        NWAFU_LOGIN_URL,
        "http://172.26.8.11/",
        "http://172.26.8.11/index_1.html",
        "http://x/eportal/portal/login",
        "",
        "not a url",
    ],
)
def test_login_page_is_not_authenticated(url: str) -> None:
    assert looks_already_authenticated(url) is False


def test_success_marker_is_exact_not_substring_of_login() -> None:
    """/success 标志不能把 srun_portal_pc（登录页）误判成已认证。"""
    assert looks_already_authenticated(NWAFU_LOGIN_URL) is False
    assert looks_already_authenticated(NWAFU_SUCCESS_URL) is True


def test_none_url_is_safe() -> None:
    assert looks_already_authenticated("") is False


# ---------- LOGIN_FORM_READY_SELECTORS：不能拿 #username 当判据 ----------


def test_ready_selector_excludes_username() -> None:
    """已认证成功页上有个 id="username" 的非 input 元素。

    拿它当判据会把成功页误判成登录页，然后拿着不存在的表单去填字段。
    """
    assert "#username" not in portal.LOGIN_FORM_READY_SELECTOR
    assert 'input[type="password"]' in portal.LOGIN_FORM_READY_SELECTOR


def test_ready_selector_accepts_password_box() -> None:
    page = _RenderPage(password_ready=True)
    assert page.locator(portal.LOGIN_FORM_READY_SELECTOR).count() == 1


def test_ready_selector_rejects_username_only_page() -> None:
    """只有 id=username 的成功页不该被判为登录表单就绪。

    已认证页上那个 #username 是显示账号的非 input 元素，
    拿它当判据就会把成功页当成登录页，然后去填不存在的表单。
    """
    page = _RenderPage(password_ready=False, username_only=True)
    assert page.locator("#username").count() == 1, "成功页上确实有 #username"
    assert page.locator(portal.LOGIN_FORM_READY_SELECTOR).count() == 0


# ---------- 可见可编辑判定 ----------


class _Loc:
    def __init__(self, count: int, fillable: bool = True) -> None:
        self._count = count
        self._fillable = fillable

    def count(self) -> int:
        return self._count

    def nth(self, index: int) -> "_Loc":
        return self

    def is_visible(self) -> bool:
        return self._fillable

    def is_editable(self) -> bool:
        return self._fillable


class _DomPage:
    """按选择器集合回答「存在 / 可填」的假页面。"""

    def __init__(self, present: tuple[str, ...] = (), fillable: tuple[str, ...] = ()) -> None:
        self.present = set(present)
        self.fillable = set(fillable)

    def locator(self, selector: str):
        base = selector.split(">> nth=")[0]
        if base in self.fillable:
            return _Loc(1, fillable=True)
        if base in self.present:
            return _Loc(1, fillable=False)
        return _Loc(0, fillable=False)


def test_hidden_fields_are_not_fillable() -> None:
    """user_ip / user_mac 这类 hidden 域存在但填不进字。"""
    page = _DomPage(present=('[name*=user]',), fillable=())
    assert _is_fillable(page, "[name*=user]") is False
    assert _first_existing(page, ["[name*=user]"]) == ""


def test_visible_text_box_is_fillable() -> None:
    page = _DomPage(present=("input[type=text]",), fillable=("input[type=text]",))
    assert _is_fillable(page, "input[type=text]") is True


def test_first_existing_skips_hidden_and_picks_fillable() -> None:
    page = _DomPage(
        present=('[name*=user]', "input[type=text]"),
        fillable=("input[type=text]",),
    )
    got = _first_existing(page, ["[name*=user]", "input[type=text]"])
    assert got == "input[type=text]"


def test_first_existing_returns_empty_when_all_hidden() -> None:
    page = _DomPage(present=('[name*=user]',), fillable=())
    assert _first_existing(page, ["[name*=user]"]) == ""


def test_first_existing_skips_invalid_selector() -> None:
    class _Boom:
        def locator(self, _selector: str):
            raise ValueError("selector invalid")

    # 非法选择器不能中断探测，直接跳到下一个
    assert _first_existing(_Boom(), ["!!!bad!!!"]) == ""


# ---------- 提交按钮选择器 ----------


def test_submit_selector_targets_h3c_button_id() -> None:
    """西农大的按钮是 <button type="button" id="login-account">。

    既不是 submit 类型也无 login-btn 类，
    只能靠链尾裸 button 撞上——顺序一变就点错按钮。
    """
    page = _DomPage()
    got = _browser_selectors(page, "", "")
    assert got["submit"].startswith("#login-account")


def test_submit_selector_keeps_legacy_candidates() -> None:
    """老门户的 input[type=submit] 不能因此丢掉。"""
    page = _DomPage()
    submit = _browser_selectors(page, "", "")["submit"]
    assert "input[type=submit]" in submit
    assert "#loginSubmitBtn" in submit
    assert ".login-btn" in submit


# ---------- JS 渲染时序 ----------


class _RenderPage:
    """模拟 JS 延迟渲染：第 ready_after 次轮询后表单才出现。

    只给 browser_login 走到的那些方法，够用即可——
    这类页面的用途是「成功页早退」和「表单永不就绪」两条分支。
    """

    def __init__(
        self,
        ready_after: int = 0,
        password_ready: bool = False,
        username_only: bool = False,
        url: str = NWAFU_LOGIN_URL,
    ) -> None:
        self.polls = 0
        self.ready_after = ready_after
        self.password_ready = password_ready
        self.username_only = username_only
        self.url = url
        self.waited: list[int] = []
        self.fills: list[tuple[str, str]] = []
        self.clicks: list[str] = []

    def locator(self, selector: str):
        ready = self.polls >= self.ready_after
        if selector == portal.LOGIN_FORM_READY_SELECTOR:
            return _Loc(1 if (self.password_ready and ready) else 0)
        if selector == "#username" and self.username_only and ready:
            return _Loc(1)  # 成功页上那个非 input 的 #username
        return _Loc(0)

    def goto(self, _url, wait_until=None, timeout=None):
        pass

    def fill(self, selector, value, timeout=None):
        self.fills.append((selector, value))

    def click(self, selector, timeout=None):
        self.clicks.append(selector)

    def content(self):
        return "ok"

    def wait_for_timeout(self, ms: int) -> None:
        self.waited.append(ms)
        self.polls += 1

    @property
    def keyboard(self):
        return self

    def type(self, _value):
        pass


def test_waits_for_js_rendered_form() -> None:
    page = _RenderPage(ready_after=3, password_ready=True)
    assert _wait_for_login_form(page, timeout=5) is True
    assert page.polls >= 3


def test_returns_false_when_form_never_renders() -> None:
    page = _RenderPage(ready_after=999, password_ready=False)
    assert _wait_for_login_form(page, timeout=0.5) is False


def test_returns_false_immediately_on_success_url() -> None:
    """门户按 IP 判定会话时直接跳成功页，不该死等表单。"""
    page = _RenderPage(ready_after=999, password_ready=False, url=NWAFU_SUCCESS_URL)
    assert _wait_for_login_form(page, timeout=30) is False
    assert page.polls <= 1, f"成功页应立即返回，实际轮询 {page.polls} 次"


def test_poll_interval_backs_off() -> None:
    page = _RenderPage(ready_after=5, password_ready=True)
    _wait_for_login_form(page, timeout=5)
    assert len(page.waited) >= 5
    assert page.waited[0] <= page.waited[-1], "间隔应单调不降"


def test_poll_interval_capped() -> None:
    page = _RenderPage(ready_after=40, password_ready=True)
    _wait_for_login_form(page, timeout=6)
    assert max(page.waited) <= 1500, f"单次等待应封顶 1.5s，实际 {max(page.waited)}ms"


# ---------- browser_login 集成桩 ----------


def _install_fake_playwright(monkeypatch: pytest.MonkeyPatch, page) -> dict:  # noqa: ANN001
    captured: dict = {}

    class _Ctx:
        def __init__(self) -> None:
            self.closed = False

        def new_page(self):
            return page

        def close(self):
            self.closed = True

    class _Browser:
        def __init__(self) -> None:
            self.closed = False
            self.ctx = _Ctx()

        def new_context(self, user_agent=None):
            return self.ctx

        def close(self):
            self.closed = True

    class _Chromium:
        def launch(self, headless=True):
            captured["headless"] = headless
            browser = _Browser()
            captured["browser"] = browser
            return browser

    class _PW:
        chromium = _Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: _PW()  # type: ignore[attr-defined]
    package = types.ModuleType("playwright")
    package.sync_api = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return captured


def _patch_probe(monkeypatch: pytest.MonkeyPatch, online: bool) -> None:
    from campusnet import probe as probe_mod

    monkeypatch.setattr(
        probe_mod,
        "probe",
        lambda url, timeout=5.0, insecure=False: probe_mod.ProbeResult(
            online, "online" if online else "redirected", "detail", url
        ),
    )


class _FullPage:
    """登录表单已就绪的页面桩，记录 fill 收到的值。"""

    def __init__(self, url: str = NWAFU_LOGIN_URL, content: str = "网络准入认证") -> None:
        self.url = url
        self.fills: list[tuple[str, str]] = []
        self.clicks: list[str] = []

    def goto(self, _url, wait_until=None, timeout=None):
        pass

    def locator(self, _selector: str):
        return _Loc(1)

    def fill(self, selector, value, timeout=None):
        self.fills.append((selector, value))

    def click(self, selector, timeout=None):
        self.clicks.append(selector)

    def content(self):
        return self.fills and "ok" or "ok"

    def wait_for_timeout(self, _ms):
        pass

    @property
    def keyboard(self):
        return self

    def type(self, _value):
        pass


def test_already_authenticated_returns_already_online(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """门户跳成功页时应报 already_online，不该尝试填不存在的表单。"""
    page = _RenderPage(ready_after=999, password_ready=False, url=NWAFU_SUCCESS_URL)
    account = Account(
        name="西农大",
        portal_url=NWAFU_LOGIN_URL,
        probe_url="http://127.0.0.1:9/probe",
        username="2024012146",
        password="secret",
        login_mode="browser",
    )
    _install_fake_playwright(monkeypatch, page)
    _patch_probe(monkeypatch, online=True)

    result = browser_login(account)

    assert result.state == "already_online"
    assert "已认证" in result.message or "无需重复登录" in result.message


def test_unknown_page_without_form_reports_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """既不是成功页也没有表单，要如实报错而不是硬填。"""
    page = _RenderPage(ready_after=999, password_ready=False, url="http://x/unknown")
    account = Account(
        name="西农大",
        portal_url="http://x/unknown",
        probe_url="http://127.0.0.1:9/probe",
        username="u",
        password="p",
        login_mode="browser",
    )
    _install_fake_playwright(monkeypatch, page)
    _patch_probe(monkeypatch, online=True)

    result = browser_login(account)

    assert result.state == "error"
    assert "渲染" in result.message or "表单" in result.message


def test_browser_login_submits_encoded_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """浏览器模式必须提交 password_encoded，不能送明文。

    base64 门户（Dr.COM 系）收明文一律报密码错误，
    而本项目的主目标场景正是这类门户。
    """
    page = _FullPage()
    account = Account(
        name="Dr.COM",
        portal_url="http://x/login",
        probe_url="http://127.0.0.1:9/probe",
        username="20240001",
        password="campus@2024",
        password_mode="base64",
        username_field="DDDDD",
        password_field="upass",
        login_mode="browser",
    )
    _install_fake_playwright(monkeypatch, page)
    _patch_probe(monkeypatch, online=True)

    browser_login(account)

    values = [v for _s, v in page.fills]
    assert "campus@2024" not in values, "浏览器模式不应送明文密码"
    expected = base64.b64encode(b"campus@2024").decode()
    assert expected in values, f"应提交 Base64 密码，实际 {values}"


def test_browser_login_plain_mode_sends_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    """plain 模式下 password_encoded 就是明文，别过度编码。"""
    page = _FullPage()
    account = Account(
        name="普通门户",
        portal_url="http://x/login",
        probe_url="http://127.0.0.1:9/probe",
        username="u",
        password="plainpw",
        password_mode="plain",
        username_field="user",
        password_field="pass",
        login_mode="browser",
    )
    _install_fake_playwright(monkeypatch, page)
    _patch_probe(monkeypatch, online=True)

    browser_login(account)

    values = [v for _s, v in page.fills]
    assert "plainpw" in values


def test_browser_login_closes_browser_on_already_online(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """already_online 是早退分支，同样不能漏关浏览器。"""
    page = _RenderPage(ready_after=999, password_ready=False, url=NWAFU_SUCCESS_URL)
    account = Account(
        name="西农大",
        portal_url=NWAFU_LOGIN_URL,
        probe_url="http://127.0.0.1:9/probe",
        username="u",
        password="p",
        login_mode="browser",
    )
    captured = _install_fake_playwright(monkeypatch, page)
    _patch_probe(monkeypatch, online=True)

    browser_login(account)

    assert captured["browser"].closed is True
    assert captured["browser"].ctx.closed is True


def test_browser_login_progress_reports_already_online(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _RenderPage(ready_after=999, password_ready=False, url=NWAFU_SUCCESS_URL)
    account = Account(
        name="西农大",
        portal_url=NWAFU_LOGIN_URL,
        probe_url="http://127.0.0.1:9/probe",
        username="u",
        password="p",
        login_mode="browser",
    )
    _install_fake_playwright(monkeypatch, page)
    _patch_probe(monkeypatch, online=True)

    seen: list[tuple[str, str]] = []
    browser_login(account, on_progress=lambda s, m: seen.append((s, m)))

    assert any("已认证" in m or "无需重复登录" in m for _s, m in seen)