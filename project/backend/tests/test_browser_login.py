"""browser_login 测试：用假的 playwright 模块驱动，不装 Chromium。

之前 portal.py 覆盖率 64%，缺口正好是整条 browser_login，
而 README 把「适配验证码 / CAS」写成卖点。逻辑层不验证就没有底气。
"""

from __future__ import annotations

import sys
import types

import pytest

from campusnet import portal
from campusnet.portal import (
    _candidate_selectors,
    _selector_for,
    browser_login,
    classify_response,
)
from campusnet.storage import Account


# ---------- 选择器映射（纯函数） ----------


def test_candidate_selectors_cover_common_shapes() -> None:
    got = _candidate_selectors("DDDDD")
    assert got[0] == 'input[name="DDDDD"]'
    assert got[1] == 'input[id="DDDDD"]'
    assert len(got) == 5


def test_candidate_selectors_escape_id_for_css() -> None:
    """字段名带特殊字符时必须转义，否则选择器非法。"""
    got = _candidate_selectors("0MKKey")
    assert got[2] == "input#0MKKey"
    got = _candidate_selectors("a.b")
    assert "\\." in got[2], f"点号应被转义，实际 {got[2]}"


def test_selector_for_prefers_explicit_field() -> None:
    assert _selector_for("DDDDD", portal.CREDENTIAL_HINTS) == 'input[name="DDDDD"]'


def test_selector_for_falls_back_to_first_hint() -> None:
    """hints 是按优先级排的，取第一个就是最优先的那个。"""
    assert _selector_for("", ("user", "name")) == 'input[name="user"]'
    assert _selector_for("", ("user", "name")) != 'input[name="name"]'
    # 顺序敏感：调用方靠排列表达优先级
    assert _selector_for("", ("name", "user")) == 'input[name="name"]'


def test_selector_for_returns_empty_when_no_hint() -> None:
    assert _selector_for("", ()) == ""


def test_browser_selectors_password_keeps_type_fallback() -> None:
    """密码选择器必须带 type=password 兜底。

    PASSWORD_HINTS 永远能猜出一个名字，`pwd or 兜底` 是死代码；
    正确做法是用逗号把两者串起来让浏览器依次尝试。
    """
    got = portal._browser_selectors(None, "", "")
    assert got["pass"] == 'input[name="pass"], input[type="password"]'
    assert "submit" in got and got["submit"]


def test_browser_selectors_uses_named_fields() -> None:
    got = portal._browser_selectors(None, "DDDDD", "upass")
    assert got["user"] == 'input[name="DDDDD"]'
    # 命名字段优先，但 type=password 仍留在链上
    assert got["pass"].startswith('input[name="upass"]')
    assert 'input[type="password"]' in got["pass"]


# ---------- playwright stub ----------


class _Page:
    """记录所有调用的假页面。"""

    def __init__(self, fail_fill=(), fail_click=(), content='{"result":1}', url="http://10.0.0.1/ok"):
        self.calls: list[tuple] = []
        self.fail_fill = set(fail_fill)
        self.fail_click = set(fail_click)
        self._content = content
        self.url = url

    def goto(self, url, wait_until=None, timeout=None):
        self.calls.append(("goto", url, wait_until, timeout))

    def fill(self, selector, value, timeout=None):
        self.calls.append(("fill", selector, value))
        if selector in self.fail_fill:
            raise RuntimeError(f"fill failed: {selector}")

    def click(self, selector, timeout=None):
        self.calls.append(("click", selector))
        if selector in self.fail_click:
            raise RuntimeError(f"click failed: {selector}")

    def content(self):
        return self._content

    def wait_for_timeout(self, ms):
        self.calls.append(("wait", ms))

    @property
    def keyboard(self):
        calls = self.calls

        class _KB:
            def type(self, value, **_k):
                calls.append(("type", value))

        return _KB()


class _Context:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class _Browser:
    def __init__(self, page, headless):
        self.ctx = _Context(page)
        self.headless = headless
        self.closed = False

    def new_context(self, user_agent=None):
        self.ua = user_agent
        return self.ctx

    def close(self):
        self.closed = True


def _install_fake_playwright(monkeypatch: pytest.MonkeyPatch, page: _Page, captured: dict):
    """把假 playwright 塞进 sys.modules，让 browser_login 能 import 到。"""

    class _Chromium:
        def launch(self, headless=True):
            captured["headless"] = headless
            browser = _Browser(page, headless)
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


@pytest.fixture()
def account() -> Account:
    return Account(
        name="浏览器账号",
        portal_url="http://10.0.0.1/eportal/portal/login",
        probe_url="http://127.0.0.1:9/probe",
        username="20240001",
        password="campus@2024",
        username_field="DDDDD",
        password_field="upass",
        login_mode="browser",
    )


def _patch_probe(monkeypatch: pytest.MonkeyPatch, online: bool) -> None:
    from campusnet import probe as probe_mod

    monkeypatch.setattr(
        probe_mod,
        "probe",
        lambda url, timeout=5.0, insecure=False: probe_mod.ProbeResult(
            online, "online" if online else "redirected", "detail", url
        ),
    )


# ---------- 浏览器登录 ----------


def test_missing_playwright_returns_actionable_error(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没装 playwright 时要给出可执行的修复命令。"""
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    result = browser_login(account)
    assert result.state == "error"
    assert "playwright install chromium" in result.message


def test_empty_portal_url_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    account = Account(name="x", portal_url="", username="u", password="p")
    result = browser_login(account)
    assert result.state == "error"
    assert "门户地址" in result.message


def test_browser_login_success(monkeypatch: pytest.MonkeyPatch, account: Account) -> None:
    page = _Page(content='{"result":1}', url="http://10.0.0.1/welcome")
    captured: dict = {}
    _install_fake_playwright(monkeypatch, page, captured)
    _patch_probe(monkeypatch, online=True)

    result = browser_login(account)

    assert result.state == "success"
    assert "探活通过" in result.message
    assert captured["headless"] is True
    assert captured["browser"].closed is True

    kinds = [c[0] for c in page.calls]
    assert kinds[0] == "goto"
    assert "fill" in kinds and "click" in kinds


def test_browser_login_headless_flag_passes_through(
    monkeypatch: pytest.MonkeyPatch, account: Account
) -> None:
    page = _Page(content='{"result":1}', url="http://10.0.0.1/ok")
    captured: dict = {}
    _install_fake_playwright(monkeypatch, page, captured)
    _patch_probe(monkeypatch, online=True)

    browser_login(account, headless=False)
    assert captured["headless"] is False


def test_browser_login_fills_both_fields(monkeypatch: pytest.MonkeyPatch, account: Account) -> None:
    page = _Page(content='{"result":1}', url="http://10.0.0.1/ok")
    _install_fake_playwright(monkeypatch, page, {})
    _patch_probe(monkeypatch, online=True)

    browser_login(account)

    filled = {c[1]: c[2] for c in page.calls if c[0] == "fill"}
    assert filled['input[name="DDDDD"]'] == "20240001"
    # 密码选择器带 type=password 兜底，键是整条逗号链
    assert filled['input[name="upass"], input[type="password"]'] == "campus@2024"


def test_browser_login_fills_extra_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """隐藏域必须填，门户才会认。"""
    account = Account(
        name="带隐藏域",
        portal_url="http://10.0.0.1/login",
        probe_url="http://127.0.0.1:9/probe",
        username="u",
        password="p",
        username_field="user",
        password_field="pass",
        extra_fields={"0MKKey": "1", "user_ip": "1.1.1.1"},
        login_mode="browser",
    )
    page = _Page(content='{"result":1}', url="http://10.0.0.1/ok")
    _install_fake_playwright(monkeypatch, page, {})
    _patch_probe(monkeypatch, online=True)

    browser_login(account)

    filled = {c[1]: c[2] for c in page.calls if c[0] == "fill"}
    assert filled.get('input[name="0MKKey"]') == "1"
    assert filled.get('input[name="user_ip"]') == "1.1.1.1"


def test_browser_login_falls_back_to_keyboard_when_fill_fails(
    monkeypatch: pytest.MonkeyPatch, account: Account
) -> None:
    """有些门户的输入框不接受 fill，得靠点击后敲键盘。"""
    page = _Page(fail_fill=['input[name="DDDDD"]'], content='{"result":1}', url="http://x/ok")
    _install_fake_playwright(monkeypatch, page, {})
    _patch_probe(monkeypatch, online=True)

    result = browser_login(account)

    assert result.state == "success"
    kinds = [c for c in page.calls if c[0] in ("click", "type")]
    assert any(c[0] == "click" for c in kinds)
    assert ("type", "20240001") in page.calls


def test_browser_login_errors_when_both_fill_methods_fail(
    monkeypatch: pytest.MonkeyPatch, account: Account
) -> None:
    """fill 与键盘都失败时报错，不能静默跳过该字段。"""
    page = _Page(
        fail_fill=['input[name="DDDDD"]'],
        fail_click=['input[name="DDDDD"]'],
        content='{"result":1}',
    )
    _install_fake_playwright(monkeypatch, page, {})
    _patch_probe(monkeypatch, online=True)

    result = browser_login(account)
    assert result.state == "error"
    assert "填写字段失败" in result.message


SUBMIT_SELECTOR = 'input[type="submit"], button[type="submit"], #loginSubmitBtn, .login-btn, button'


def test_browser_login_reports_submit_click_failure(
    monkeypatch: pytest.MonkeyPatch, account: Account
) -> None:
    page = _Page(fail_click=[SUBMIT_SELECTOR])
    _install_fake_playwright(monkeypatch, page, {})
    _patch_probe(monkeypatch, online=True)

    result = browser_login(account)
    assert result.state == "error"
    assert "点击登录按钮失败" in result.message


def test_browser_login_password_error(monkeypatch: pytest.MonkeyPatch, account: Account) -> None:
    page = _Page(content="用户名或密码错误", url="http://10.0.0.1/eportal/portal/login")
    _install_fake_playwright(monkeypatch, page, {})
    _patch_probe(monkeypatch, online=True)

    result = browser_login(account)
    assert result.state == "failed"


def test_browser_login_still_on_login_page_fails(
    monkeypatch: pytest.MonkeyPatch, account: Account
) -> None:
    """内容无失败标记但 URL 仍在登录页，应判失败而非成功。"""
    page = _Page(content="<html>请登录</html>", url="http://10.0.0.1/eportal/portal/login")
    _install_fake_playwright(monkeypatch, page, {})
    _patch_probe(monkeypatch, online=True)

    result = browser_login(account)
    assert result.state == "failed"


def test_browser_login_reports_probe_not_released(
    monkeypatch: pytest.MonkeyPatch, account: Account
) -> None:
    """门户说成功但探活未放行，要如实告知，不能报成功。"""
    page = _Page(content='{"result":1}', url="http://10.0.0.1/ok")
    _install_fake_playwright(monkeypatch, page, {})
    _patch_probe(monkeypatch, online=False)

    result = browser_login(account)
    assert "未放行" in result.message


def test_browser_login_closes_context_and_browser(
    monkeypatch: pytest.MonkeyPatch, account: Account
) -> None:
    """浏览器必须关闭，否则长期运行会堆积 Chromium 进程。"""
    page = _Page(content='{"result":1}', url="http://x/ok")
    captured: dict = {}
    _install_fake_playwright(monkeypatch, page, captured)
    _patch_probe(monkeypatch, online=True)

    browser_login(account)

    browser = captured["browser"]
    assert browser.closed is True
    assert browser.ctx.closed is True


def test_browser_login_closes_browser_even_when_submit_fails(
    monkeypatch: pytest.MonkeyPatch, account: Account
) -> None:
    """点击失败走的是早退分支，同样不能漏关浏览器。"""
    page = _Page(fail_click=["input[type=submit], button[type=submit], #loginSubmitBtn, .login-btn, button"])
    captured: dict = {}
    _install_fake_playwright(monkeypatch, page, captured)
    _patch_probe(monkeypatch, online=True)

    browser_login(account)
    assert captured["browser"].closed is True


def test_browser_login_passes_user_agent(monkeypatch: pytest.MonkeyPatch, account: Account) -> None:
    page = _Page(content='{"result":1}', url="http://x/ok")
    captured: dict = {}
    _install_fake_playwright(monkeypatch, page, captured)
    _patch_probe(monkeypatch, online=True)

    browser_login(account)
    assert "Chrome" in captured["browser"].ua


def test_browser_login_uses_generous_goto_timeout(
    monkeypatch: pytest.MonkeyPatch, account: Account
) -> None:
    """goto 超时要按秒转毫秒，且不能小于 1 秒。"""
    page = _Page(content='{"result":1}', url="http://x/ok")
    _install_fake_playwright(monkeypatch, page, {})
    _patch_probe(monkeypatch, online=True)

    browser_login(account, timeout=30.0)
    goto = next(c for c in page.calls if c[0] == "goto")
    assert goto[3] == 30000


def test_browser_login_emits_progress(monkeypatch: pytest.MonkeyPatch, account: Account) -> None:
    """浏览器模式可能要跑十几秒，必须有进度反馈。"""
    page = _Page(content='{"result":1}', url="http://x/ok")
    _install_fake_playwright(monkeypatch, page, {})
    _patch_probe(monkeypatch, online=True)

    seen: list[tuple[str, str]] = []
    browser_login(account, on_progress=lambda s, m: seen.append((s, m)))

    stages = [s for s, _ in seen]
    assert "browser" in stages
    assert "connect" in stages
    assert any("启动浏览器" in m for _, m in seen)
    assert any("打开门户" in m for _, m in seen)


def test_browser_login_without_callback_is_safe(
    monkeypatch: pytest.MonkeyPatch, account: Account
) -> None:
    page = _Page(content='{"result":1}', url="http://x/ok")
    _install_fake_playwright(monkeypatch, page, {})
    _patch_probe(monkeypatch, online=True)

    assert browser_login(account).state == "success"


def test_browser_login_waits_after_submit(monkeypatch: pytest.MonkeyPatch, account: Account) -> None:
    """点完登录要等页面跳转，否则过早取 content。"""
    page = _Page(content='{"result":1}', url="http://x/ok")
    _install_fake_playwright(monkeypatch, page, {})
    _patch_probe(monkeypatch, online=True)

    browser_login(account)
    assert any(c[0] == "wait" for c in page.calls)


def test_browser_login_tolerates_wait_failure(
    monkeypatch: pytest.MonkeyPatch, account: Account
) -> None:
    """wait_for_timeout 失败不该毁掉整次登录。"""
    page = _Page(content='{"result":1}', url="http://x/ok")
    _install_fake_playwright(monkeypatch, page, {})
    _patch_probe(monkeypatch, online=True)

    def boom(_ms):
        raise RuntimeError("navigation interrupted")

    page.wait_for_timeout = boom  # type: ignore[method-assign]

    assert browser_login(account).state == "success"


# ---------- login 分发 ----------


def test_login_routes_browser_mode(account: Account, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict = {}

    def spy(acc, headless=True, timeout=30.0, on_progress=None):  # noqa: ANN001
        called["mode"] = acc.login_mode
        called["headless"] = headless
        return portal.LoginResult("success", "ok")

    monkeypatch.setattr(portal, "browser_login", spy)
    portal.login(account, headless=False)
    assert called["mode"] == "browser"
    assert called["headless"] is False


def test_login_routes_manual_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    account = Account(
        name="人工", portal_url="http://x", probe_url="http://127.0.0.1:9/p",
        username="u", password="p", login_mode="manual",
    )
    _patch_probe(monkeypatch, online=True)
    result = portal.login(account)
    assert result.state == "already_online"

    _patch_probe(monkeypatch, online=False)
    result = portal.login(account)
    assert result.state == "error"
    assert "手动" in result.message


def test_login_manual_mode_emits_no_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """人工模式没有可自动化的阶段，不该发进度。"""
    account = Account(
        name="人工", portal_url="http://x", probe_url="http://127.0.0.1:9/p",
        username="u", password="p", login_mode="manual",
    )
    _patch_probe(monkeypatch, online=False)
    seen: list = []
    portal.login(account, on_progress=lambda s, m: seen.append(s))
    assert seen == []


def test_login_routes_http_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    account = Account(
        name="http", portal_url="http://x", probe_url="http://127.0.0.1:9/p",
        username="u", password="p", login_mode="http",
    )
    seen: list = []

    def spy(acc, timeout=10.0, insecure=False, on_progress=None):  # noqa: ANN001
        if on_progress:
            on_progress("connect", "正在连接")
        return portal.LoginResult("success", "ok")

    monkeypatch.setattr(portal, "http_login", spy)
    result = portal.login(account, on_progress=lambda s, m: seen.append(s))
    assert result.state == "success"
    assert seen == ["connect"]


def test_login_defaults_to_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """没写 login_mode 时按 http 处理。"""
    account = Account(
        name="默认", portal_url="http://x", probe_url="http://127.0.0.1:9/p",
        username="u", password="p", login_mode="",
    )
    called = {}
    monkeypatch.setattr(
        portal, "http_login", lambda *a, **k: called.setdefault("hit", True) or portal.LoginResult("success", "")
    )
    portal.login(account)
    assert called.get("hit") is True


# ---------- 响应分类 ----------


@pytest.mark.parametrize(
    "content,status,url,expected",
    [
        ('{"result":1}', 200, "http://x/ok", "success"),
        ('{"success": true}', 200, "http://x/ok", "success"),
        ("您已在线", 200, "http://x/", "already_online"),
        ("密码错误", 200, "http://x/", "failed"),
        ('{"result":0}', 200, "http://x/", "failed"),
        ("<html>表单</html>", 200, "http://10.0.0.1/eportal/portal/login", "failed"),
        ("<html>欢迎</html>", 200, "http://x/welcome", "success"),
        ("err", 500, "http://x/", "error"),
    ],
)
def test_classify_response_matrix(content: str, status: int, url: str, expected: str) -> None:
    assert classify_response(content, status, url).state == expected
