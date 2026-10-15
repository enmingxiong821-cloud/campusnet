"""Opt-in real browser checks; system startup operations remain mocked.

Run with CAMPUSNET_BROWSER_CHANNEL=chrome (or chromium for bundled browsers).
"""

import os
import threading
from http.server import ThreadingHTTPServer

import pytest

from campusnet import bootstart, server
from campusnet.monitor import MonitorManager
from campusnet.storage import Account


@pytest.mark.parametrize("width", [1280, 390])
def test_bootstart_interaction(store, monkeypatch, width):
    channel = os.environ.get("CAMPUSNET_BROWSER_CHANNEL")
    if not channel:
        pytest.skip("Set CAMPUSNET_BROWSER_CHANNEL to enable real browser checks")
    from playwright.sync_api import expect, sync_playwright

    state = {"installed": False, "fail": True}
    calls = []

    def status():
        return bootstart.BootStatus(True, state["installed"], None, "test status")

    def install(**kwargs):
        calls.append("install")
        state["installed"] = True
        return status()

    def uninstall():
        calls.append("uninstall")
        if state["fail"]:
            return bootstart.BootStatus(True, True, None, "simulated permission failure", False)
        state["installed"] = False
        return status()

    monkeypatch.setattr(bootstart, "status", status)
    monkeypatch.setattr(bootstart, "install", install)
    monkeypatch.setattr(bootstart, "uninstall", uninstall)
    account_id = store.add_account(Account(name="状态测试", username="test", password="test", portal_url="http://example.invalid"))
    handler = server.build_handler(store, MonitorManager())
    server._install_dispatch(handler)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=channel, headless=True)
            try:
                page = browser.new_page(viewport={"width": width, "height": 844})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{httpd.server_port}/")
                expect(page.locator('#accountTable .badge')).to_have_text('未检测')
                expect(page.locator('#cOfflineSub')).to_have_text('1 个未检测')
                store.set_state(account_id, 'unknown')
                page.reload()
                expect(page.locator('#accountTable .badge')).to_have_text('在线未确认')
                expect(page.locator('#cOfflineSub')).to_have_text('1 个在线未确认')
                page.route('**/api/check', lambda route: route.fulfill(json={
                    'ok': True, 'results': [{'online': False, 'state': 'unknown'}],
                }))
                page.locator('#btnProbe').click()
                expect(page.locator('#toast')).to_contain_text('1 个账号，1 个在线未确认')
                expect(page.locator('#toast')).not_to_contain_text('掉线')
                page.locator('[data-tab="settings"]').click()
                badge = page.locator("#bootBadge")
                enable = page.locator("#btnBootInstall")
                disable = page.locator("#btnBootUninstall")
                expect(badge).to_have_text("未开启")
                enable.click()
                expect(badge).to_have_text("已开启")
                expect(enable).to_be_disabled()
                with page.expect_response("**/api/bootstart/uninstall") as response:
                    disable.click()
                assert response.value.status == 500
                response_body = response.value.text()
                assert 'simulated permission failure' in response_body
                try:
                    expect(disable).to_be_enabled()
                except AssertionError as exc:
                    raise AssertionError(f"errors={errors}; hint={page.locator('#bootHint').inner_text()}") from exc
                expect(badge).to_have_text("已开启")
                expect(page.get_by_text("操作失败: simulated permission failure", exact=False).first).to_be_visible()
                state["fail"] = False
                disable.click()
                expect(badge).to_have_text("未开启")
                expect(enable).to_be_enabled()
                expect(disable).to_be_disabled()
                assert calls == ["install", "uninstall", "uninstall"]
                assert errors == []
            finally:
                browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_console_keeps_unsaved_settings_and_clears_trimmed_toast_records(store):
    """两条纯前端的守卫，都不需要后端出错就能复现。

    1. 切走再切回设置页会重新拉一次 `/api/settings` 回填表单，用户刚改的值被无声
       覆盖，既没有报错也没有提示——于是这里断言值还在，并且给了那句提示。
    2. 同屏超过 5 条 toast 时会强制移除最旧的一条，只 `removeChild` 而不清记录与
       定时器，于是同一个 key 在随后 3.8 秒内再报一次会命中「已存在」分支，去更新
       一个已经脱离文档的节点——消息被静默吞掉，屏幕上什么都不出现。
    """
    channel = os.environ.get("CAMPUSNET_BROWSER_CHANNEL")
    if not channel:
        pytest.skip("Set CAMPUSNET_BROWSER_CHANNEL to enable real browser checks")
    from playwright.sync_api import expect, sync_playwright

    handler = server.build_handler(store, MonitorManager())
    server._install_dispatch(handler)
    # 拿一个与将要填的值不同的老值当锚点：等回填落地再改，免得那一手把 33 冲掉。
    store.set_setting("interval", 45)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=channel, headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 844})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{httpd.server_port}/")
                page.locator('[data-tab="settings"]').click()
                page.wait_for_function(
                    "() => document.querySelector('#setInterval').value === '45'"
                )

                page.fill("#setInterval", "33")
                page.locator('[data-tab="accounts"]').click()
                page.locator('[data-tab="settings"]').click()
                # 提示出现即说明 loadSettings 走到了守卫分支（它在回填之前 return）
                expect(page.locator("#toast")).to_contain_text("未保存")
                assert page.input_value("#setInterval") == "33"
                assert store.all_settings()["interval"] == 45

                enough = page.evaluate(
                    """() => {
                        const box = document.querySelector('#toast');
                        box.innerHTML = '';   // 与前面的提示脱钩，下面只用全新的 key
                        for (let i = 0; i < 6; i++) toast('m' + i, '', 'trim' + i);
                        return box.children.length;
                    }"""
                )
                assert enough == 5, enough   # 超出 5 条立刻清掉最旧的，'trim0' 被移除
                page.evaluate("() => toast('again', '', 'trim0')")
                # 记录没清干净的话，这一句会去更新那个已脱离文档的节点，
                # lastElementChild 就还是 'm5'。
                last = page.evaluate(
                    "() => document.querySelector('#toast').lastElementChild.textContent"
                )
                assert last == "again", last
                assert errors == []
            finally:
                browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_console_settings_save_persists_interval_headless_autostart(store):
    """保存设置时，间隔 / 无头 / 开机自启必须真的落库。

    这三个键原来只挂在 `monitor/start` 的请求体上，而那个请求只在监控正在跑时才发
    （没在跑的分支根本不发），于是监控没开时改它们会被静默丢弃，提示语却照样说
    「设置已保存」——用户改完、重启程序，看到的还是老值。
    """
    channel = os.environ.get("CAMPUSNET_BROWSER_CHANNEL")
    if not channel:
        pytest.skip("Set CAMPUSNET_BROWSER_CHANNEL to enable real browser checks")
    from playwright.sync_api import expect, sync_playwright

    handler = server.build_handler(store, MonitorManager())
    server._install_dispatch(handler)
    # 先把库里的值设成一组「已知且与将要改成的值都不同」的数：切到设置页会异步拉一次
    # /api/settings 回填输入框，那一手要是落在我们填完之后，就会把刚填的 33 冲掉，
    # 于是保存的还是老值——用例会随机红。下面等回填落地再改，拿这组老值当锚点。
    store.set_setting("interval", 45)
    store.set_setting("headless", True)
    store.set_setting("autostart", False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=channel, headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 844})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                started = []
                page.on(
                    "request",
                    lambda request: (
                        started.append(request.url)
                        if request.url.endswith("/api/monitor/start")
                        else None
                    ),
                )
                page.goto(f"http://127.0.0.1:{httpd.server_port}/")
                page.locator('[data-tab="settings"]').click()
                page.wait_for_function(
                    "() => document.querySelector('#setInterval').value === '45'"
                )
                page.fill("#setInterval", "33")
                page.select_option("#setHeadless", "0")
                page.select_option("#setAutostart", "1")
                # 控制台会周期性刷新，也会打 /api/settings——只按网址匹配会抓到那个
                # GET（然后就变成一条随机失败的用例），这里连方法一起判。
                with page.expect_response(
                    lambda response: response.request.method == "PUT"
                    and response.url.endswith("/api/settings")
                ) as response:
                    page.locator("#btnSaveSettings").click()
                assert response.value.status == 200

                saved = store.all_settings()
                assert saved["interval"] == 33, saved
                assert saved["headless"] is False, saved
                assert saved["autostart"] is True, saved
                # 监控没在跑，就只落库：别顺手把它开起来（无头关着时会当场弹窗口）
                assert started == []
                expect(page.locator("#toast")).to_contain_text("设置已保存")
                assert errors == []
            finally:
                browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_console_edit_account_says_whether_password_is_kept(store):
    """编辑账号时必须让用户看清密码还在不在——编辑框本来就不回显。

    后端刻意不把密码回给界面（`Account.public()` 里没有 password），所以编辑框永远是
    个空框：「原密码还在」和「密码被清空了」在屏幕上长得一模一样。用户按提示留空保存、
    再打开一看还是空的，很自然会认定「密码没保存」。这里锁住四件事：编辑态标签不带
    必填星号、占位提示说明密码已保存、保存结果明说密码动没动、没密码的账号另有一套提示。
    """
    channel = os.environ.get("CAMPUSNET_BROWSER_CHANNEL")
    if not channel:
        pytest.skip("Set CAMPUSNET_BROWSER_CHANNEL to enable real browser checks")
    from playwright.sync_api import expect, sync_playwright

    with_pw = store.add_account(
        Account(
            name="有密码",
            username="u1",
            password="OLD_PW",
            portal_url="http://10.0.0.1/eportal/portal/login",
        )
    )
    store.add_account(
        Account(
            name="没密码",
            username="u2",
            password="",
            portal_url="http://10.0.0.2/eportal/portal/login",
        )
    )
    handler = server.build_handler(store, MonitorManager())
    server._install_dispatch(handler)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=channel, headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{httpd.server_port}/")
                expect(page.locator("#accountTable tbody tr")).to_have_count(2)

                def open_edit(name):
                    page.locator("tr", has_text=name).locator(
                        'button[data-act="edit"]'
                    ).click()

                def closed():
                    page.wait_for_function(
                        "() => !document.querySelector('#modalBg').classList.contains('open')"
                    )

                # 有密码的账号：编辑态不该再标必填，占位提示说明密码还在
                open_edit("有密码")
                expect(page.locator("#lblPassword")).to_have_text("认证密码")
                expect(page.locator("#fPassword")).to_have_attribute(
                    "placeholder", "已保存，留空则不修改"
                )
                expect(page.locator("#fPassword")).to_have_value("")

                # 留空保存：原密码必须留住，提示要说明「未变动」
                page.locator("#btnSave").click()
                closed()
                expect(page.locator("#toast")).to_contain_text("已保存，密码未变动")
                kept = store.get_account(with_pw)
                assert kept is not None and kept.password == "OLD_PW"

                # 填了新密码：覆盖，提示要说明「已更新」
                open_edit("有密码")
                page.locator("#fPassword").fill("NEW_PW_123")
                page.locator("#btnSave").click()
                closed()
                expect(page.locator("#toast")).to_contain_text("已保存，密码已更新")
                changed = store.get_account(with_pw)
                assert changed is not None and changed.password == "NEW_PW_123"

                # 没密码的账号：不能沿用「留空则不修改」，那会让用户以为曾设过密码
                open_edit("没密码")
                expect(page.locator("#fPassword")).to_have_attribute(
                    "placeholder", "这个账号还没有密码"
                )

                # 添加态仍然是必填
                page.locator("#btnCancel").click()
                page.locator("#btnAdd").click()
                expect(page.locator("#lblPassword")).to_have_text("认证密码 *")
                expect(page.locator("#fPassword")).to_have_attribute("placeholder", "")

                assert errors == []
            finally:
                browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
