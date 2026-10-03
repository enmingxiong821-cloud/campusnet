"""探活逻辑单元测试（用本地 HTTP 服务模拟门户与放行/重定向行为）。"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from campusnet import probe as probe_mod


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


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


def test_probe_gzip_body_decoded(server: str) -> None:
    result = probe_mod.probe(f"{server}/gzip")
    assert result.online and "OK" in result.detail


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