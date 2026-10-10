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
