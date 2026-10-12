"""探活逻辑单元测试（用本地 HTTP 服务模拟门户与放行/重定向行为）。"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from campusnet import probe as probe_mod


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    loop_requests = 0

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/online":
            body = b"Microsoft Connect Test"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/no-content":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/portal-redirect":
            self.send_response(302)
            self.send_header("Location", "http://10.0.0.1/eportal/portal/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path in ("/relative", "/nested/relative", "/scheme-relative", "/normal-relative"):
            targets = {
                "/relative": "/srun_portal_pc?ac_id=1&theme=pro",
                "/nested/relative": "../srun_portal_pc?ac_id=1&theme=pro",
                "/scheme-relative": f"//{self.headers['Host']}/srun_portal_pc?ac_id=1&theme=pro",
                "/normal-relative": "/online",
            }
            self.send_response(302)
            self.send_header("Location", targets[path])
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/loop":
            type(self).loop_requests += 1
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path in ("/short-chain", "/short-last"):
            self.send_response(302)
            self.send_header("Location", "/short-last" if path == "/short-chain" else "/online")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/gzip":
            import gzip

            body = gzip.compress(b"OK")
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/boom":
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_probe_online(server: str) -> None:
    result = probe_mod.probe(f"{server}/online")
    assert result.online
    assert "Microsoft Connect Test" in result.detail


def test_probe_204_counts_as_online(server: str) -> None:
    assert probe_mod.probe(f"{server}/no-content").online


def test_probe_redirect_to_portal_is_offline(server: str) -> None:
    result = probe_mod.probe(f"{server}/portal-redirect")
    assert not result.online
    assert result.status == "redirected"
    assert "eportal" in result.detail


@pytest.mark.parametrize("path", ["/relative", "/nested/relative", "/scheme-relative"])
def test_relative_portal_redirect_is_absolute(server: str, path: str) -> None:
    expected = f"{server}/srun_portal_pc?ac_id=1&theme=pro"
    result = probe_mod.probe(server + path)
    assert not result.online
    assert result.status == "redirected"
    assert result.final_url == expected
    assert probe_mod.detect_portal_url(server + path) == expected


def test_normal_relative_redirect_is_followed(server: str) -> None:
    result = probe_mod.probe(server + "/normal-relative")
    assert result.online
    assert result.status == "redirected_ok"


def test_probe_gzip_body_decoded(server: str) -> None:
    result = probe_mod.probe(f"{server}/gzip")
    assert result.online and "OK" in result.detail


def test_probe_redirect_loop_is_bounded(server: str) -> None:
    """自跳转最多发送 MAX_REDIRECTS 次 HTTP 请求。"""
    _Handler.loop_requests = 0
    result = probe_mod.probe(f"{server}/loop")
    assert not result.online
    assert result.status == "redirect_loop"
    assert str(probe_mod.MAX_REDIRECTS) in result.detail
    assert _Handler.loop_requests == probe_mod.MAX_REDIRECTS


def test_probe_redirect_loop_request_count_is_capped(server: str, monkeypatch) -> None:
    calls = []
    original = probe_mod._build_opener

    def counting(insecure: bool = False):
        calls.append(1)
        return original(insecure)

    monkeypatch.setattr(probe_mod, "_build_opener", counting)
    probe_mod.probe(f"{server}/loop")
    # opener 只建一次，且层数不超过上限
    assert len(calls) == 1


def test_probe_short_redirect_chain_still_follows(server: str) -> None:
    """正常的短链必须跟到在线页，不能被深度限制误伤。"""
    result = probe_mod.probe(f"{server}/short-chain")
    assert result.online
    assert result.status == "redirected_ok"


def test_probe_http_error(server: str) -> None:
    result = probe_mod.probe(f"{server}/boom")
    assert not result.online
    assert result.status == "http_error"


def test_probe_connection_refused_is_offline() -> None:
    result = probe_mod.probe("http://127.0.0.1:9/never", timeout=2)
    assert not result.online
    assert result.status in ("unreachable", "timeout", "error")


def test_probe_empty_url_uses_default() -> None:
    result = probe_mod.probe("", timeout=2)
    assert isinstance(result.status, str)


def test_probe_never_raises() -> None:
    # 明显非法的 URL 也必须返回结果而不是抛异常
    assert isinstance(probe_mod.probe("ht!tp://%%%", timeout=1).online, bool)


def test_probe_any_stops_on_first_online(server: str) -> None:
    result = probe_mod.probe_any(("http://127.0.0.1:9/dead", f"{server}/online"), timeout=3)
    assert result.online


def test_probe_any_reports_failure(server: str) -> None:
    result = probe_mod.probe_any((f"{server}/boom",), timeout=3)
    assert not result.online


def test_is_portal_url() -> None:
    assert probe_mod.is_portal_url("http://10.0.0.1/eportal/portal/login")
    assert probe_mod.is_portal_url("http://x/DrCom/login")
    assert not probe_mod.is_portal_url("http://www.example.com/index.html")


def test_detect_portal_from_redirect(server: str) -> None:
    found = probe_mod.detect_portal_url(f"{server}/portal-redirect")
    assert found and "eportal" in found


def test_detect_portal_returns_none_when_not_portal(server: str) -> None:
    assert probe_mod.detect_portal_url(f"{server}/online") is None


def test_probe_configured_splits_targets_and_stops_at_first_online(monkeypatch) -> None:  # noqa: ANN001
    """多地址配置：按空白或逗号拆开，命中放行后不再打后面那个。"""
    seen: list[str] = []

    def fake_probe(url: str, timeout: float = 5.0, insecure: bool = False):
        seen.append(url)
        online = url.endswith("/ok")
        return probe_mod.ProbeResult(
            online, "online" if online else "unreachable", "", url
        )

    monkeypatch.setattr(probe_mod, "probe", fake_probe)
    result = probe_mod.probe_configured("http://a/blocked, http://b/ok http://c/never")

    assert result.online is True
    assert seen == ["http://a/blocked", "http://b/ok"]


def test_probe_configured_reports_last_failure(monkeypatch) -> None:  # noqa: ANN001
    """全部被拦时，结论与详情应来自最后一个地址，方便看日志定位。"""

    def fake_probe(url: str, timeout: float = 5.0, insecure: bool = False):
        return probe_mod.ProbeResult(False, "unreachable", "blocked", url)

    monkeypatch.setattr(probe_mod, "probe", fake_probe)
    result = probe_mod.probe_configured("http://a/blocked http://b/blocked")

    assert result.online is False
    assert result.final_url == "http://b/blocked"


def test_probe_configured_empty_falls_back_to_probe(monkeypatch) -> None:  # noqa: ANN001
    """留空时交给 probe() 处理，由它回落到默认探活地址。"""
    seen: list[str] = []

    def fake_probe(url: str, timeout: float = 5.0, insecure: bool = False):
        seen.append(url)
        return probe_mod.ProbeResult(True, "online", "", url)

    monkeypatch.setattr(probe_mod, "probe", fake_probe)

    assert probe_mod.probe_configured("").online is True
    assert seen == [""]
