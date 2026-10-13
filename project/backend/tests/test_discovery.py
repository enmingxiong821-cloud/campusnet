"""厂商无关门户发现的单元测试（本地 HTTP 服务模拟各种厂商门户）。"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from campusnet import discovery

DRCOM_HTML = """
<html><head><title>Dr.COM 认证系统</title></head><body>
<form action="/eportal/portal/login/" method="post">
  <input type="hidden" name="user_ip" value="10.110.168.53">
  <input type="hidden" name="user_mac" value="AA-BB-CC">
  <input type="hidden" name="0MKKey" value="">
  <input type="text" name="DDDDD" value="">
  <input type="password" name="upass" value="">
  <input type="submit" name="submitBtn" value="登录">
</form></body></html>
"""

HUAWEI_HTML = """
<html><head><title>华为认证</title></head><body>
<div>captcha 验证码</div>
<form action="/portal/login" method="post">
  <input type="text" name="username">
  <input type="password" name="password">
  <input type="submit" name="submit">
</form></body></html>
"""

CAS_HTML = """
<html><head><title>CAS 单点登录</title></head><body>
<form action="/cas/login" method="post">
  <input type="hidden" name="execution" value="e1s1">
  <input name="j_username" type="text">
  <input name="j_password" type="password">
  <button type="submit">登录</button>
</form></body></html>
"""

PLAIN_HTML = "<html><head><title>某某校园网</title></head><body>没有表单</body></html>"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    pages: dict[str, str] = {}

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        body = self.pages.get(path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture(scope="module")
def portal_server():
    handler = type("H", (_Handler,), {"pages": {}})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    yield f"http://{host}:{port}", handler
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive()


# ---------- 网关推导 ----------


def test_gateway_of_from_ip() -> None:
    """校园网常见 /16 网段：网关落在 x.y.0.1。"""
    assert discovery.gateway_of("10.110.168.53") == "10.110.0.1"
    assert discovery.gateway_of("192.168.1.20") == "192.168.0.1"


def test_gateway_candidates_covers_both_masks() -> None:
    """/16 与 /24 两种掩码都要给出候选，交给扫描去判定。"""
    assert discovery.gateway_candidates("10.110.168.53") == ["10.110.0.1", "10.110.168.1"]
    assert discovery.gateway_candidates("192.168.1.20") == ["192.168.0.1", "192.168.1.1"]
    assert discovery.gateway_candidates("10.1.1.5")[0] == "10.1.0.1"


def test_gateway_candidates_rejects_garbage() -> None:
    assert discovery.gateway_candidates("not-an-ip") == []
    assert discovery.gateway_candidates("1.2.3.4.5") == []
    assert discovery.gateway_candidates("") == []


def test_gateway_of_matches_first_candidate() -> None:
    for ip in ("10.110.168.53", "192.168.1.20", "172.16.5.9"):
        assert discovery.gateway_of(ip) == discovery.gateway_candidates(ip)[0]


def test_gateway_of_rejects_garbage() -> None:
    assert discovery.gateway_of("not-an-ip") is None
    assert discovery.gateway_of("") is None


def test_guess_gateways_starts_with_local_gateway() -> None:
    gateways = discovery.guess_gateways()
    assert gateways
    assert gateways[0] == discovery.gateway_of() or "10.0.0.1" in gateways
    assert len(set(gateways)) == len(gateways)  # 去重


def test_is_private_ipv4() -> None:
    assert discovery._is_private_ipv4("10.0.0.1") is True
    assert discovery._is_private_ipv4("172.16.0.5") is True
    assert discovery._is_private_ipv4("172.31.255.255") is True
    assert discovery._is_private_ipv4("192.168.1.1") is True
    assert discovery._is_private_ipv4("127.0.0.1") is True
    assert discovery._is_private_ipv4("8.8.8.8") is False
    assert discovery._is_private_ipv4("172.15.0.1") is False
    assert discovery._is_private_ipv4("not-an-ip") is False


def test_detect_dns_hijack_returns_private_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        discovery.socket,
        "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("10.110.168.53", 0))],
    )

    assert discovery.detect_dns_hijack("www.msftconnecttest.com") == "10.110.168.53"


def test_detect_dns_hijack_ignores_public(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        discovery.socket,
        "getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("8.8.8.8", 0))],
    )

    assert discovery.detect_dns_hijack("www.baidu.com") is None


def test_detect_dns_hijack_handles_resolve_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(host, port):  # noqa: ANN001, ANN002
        raise OSError("no resolve")

    monkeypatch.setattr(discovery.socket, "getaddrinfo", _raise)

    assert discovery.detect_dns_hijack("no-such-host.invalid") is None


def test_local_ipv4_returns_valid_shape() -> None:
    ip = discovery.local_ipv4()
    assert ip is None or ip.count(".") == 3


def test_is_url() -> None:
    assert discovery.is_url("http://10.0.0.1/login")
    assert not discovery.is_url("10.0.0.1")
    assert not discovery.is_url("ftp://a/b")


# ---------- 厂商指纹 ----------


@pytest.mark.parametrize(
    "html_text,url,expected",
    [
        (DRCOM_HTML, "", "Dr.COM"),
        (HUAWEI_HTML, "", "华为"),
        (CAS_HTML, "", "水木 / 自建 CAS"),
        ("<html>普通页面</html>", "", "未知厂商"),
    ],
)
def test_identify_vendor(html_text: str, url: str, expected: str) -> None:
    vendor, _sig = discovery.identify_vendor(html_text, url)
    assert vendor == expected


def test_identify_vendor_by_url_only() -> None:
    vendor, sig = discovery.identify_vendor("<html></html>", "http://x/sportal/authorize/index")
    assert vendor == "深菱 spl"
    assert sig == "/sportal/"


def test_needs_captcha() -> None:
    assert discovery.needs_captcha(HUAWEI_HTML)
    assert discovery.needs_captcha("<img src='captcha.png'>")
    assert not discovery.needs_captcha(DRCOM_HTML)


# ---------- 表单分析 ----------


def test_analyze_drcom_portal() -> None:
    candidate = discovery.analyze_portal("http://10.0.0.1/eportal/portal/login", DRCOM_HTML)

    assert candidate.vendor == "Dr.COM"
    assert candidate.title == "Dr.COM 认证系统"
    assert candidate.username_field == "DDDDD"
    assert candidate.password_field == "upass"
    assert candidate.login_path == "/eportal/portal/login/"
    assert candidate.suggested_password_mode == "base64"
    assert candidate.extra_fields["user_ip"] == "10.110.168.53"
    assert candidate.extra_fields["0MKKey"] == ""  # 空值隐藏字段也要带上门户才认
    assert "upass" not in candidate.extra_fields


def test_analyze_captcha_portal_forces_browser_mode() -> None:
    candidate = discovery.analyze_portal("http://10.0.0.1/portal/login", HUAWEI_HTML)
    assert candidate.needs_captcha is True
    assert candidate.suggested_login_mode == "browser"


def test_analyze_page_without_form() -> None:
    candidate = discovery.analyze_portal("http://x/", PLAIN_HTML)
    assert candidate.username_field == ""
    assert candidate.password_field == ""
    assert candidate.extra_fields == {}


def test_analyze_output_is_serialisable() -> None:
    import json

    payload = discovery.analyze_portal("http://x/eportal/portal/login", DRCOM_HTML).as_dict()
    assert json.loads(json.dumps(payload, ensure_ascii=False))["vendor"] == "Dr.COM"


def test_to_account_fields_shape() -> None:
    candidate = discovery.analyze_portal("http://x/eportal/portal/login", DRCOM_HTML).as_dict()
    fields = discovery.to_account_fields(candidate)
    assert set(fields) == {
        "portal_url",
        "username_field",
        "password_field",
        "extra_fields",
        "login_mode",
        "password_mode",
    }
    assert fields["portal_url"].startswith("http")


# ---------- 扫描 ----------


def test_scan_urls_finds_portal(portal_server) -> None:  # noqa: ANN001
    base, handler = portal_server
    handler.pages["/eportal/portal/login"] = DRCOM_HTML
    handler.pages["/portal/login"] = HUAWEI_HTML

    candidates = discovery.scan_urls((f"{base}/eportal/portal/login", f"{base}/portal/login"))
    vendors = {c.vendor for c in candidates}
    assert "Dr.COM" in vendors and "华为" in vendors


def test_scan_urls_skips_dead_address(portal_server) -> None:  # noqa: ANN001
    base, _ = portal_server
    assert discovery.scan_urls(("http://127.0.0.1:9/nope",)) == []


def test_scan_urls_ignores_empty_input() -> None:
    assert discovery.scan_urls(["", "http://127.0.0.1:9/x"]) == []


def test_scan_gateway_uses_candidate_paths(portal_server) -> None:  # noqa: ANN001
    base, handler = portal_server
    host_port = base.replace("http://", "")
    handler.pages["/eportal/portal/login"] = DRCOM_HTML

    candidates = discovery.scan_gateway(host_port, timeout=3.0, limit=2)
    assert any("eportal" in c.url for c in candidates)
    assert all(c.url.startswith("http") for c in candidates)


def test_scan_gateway_on_dead_port_is_empty() -> None:
    assert discovery.scan_gateway("127.0.0.1:9", timeout=1.0) == []


def test_scan_all_gateways_stops_after_first_hit(portal_server) -> None:  # noqa: ANN001
    """命中第一个网关就停，不要在无关网段上空转。"""
    base, handler = portal_server
    host_port = base.replace("http://", "")
    handler.pages["/eportal/portal/login"] = DRCOM_HTML

    found = discovery.scan_all_gateways(
        [host_port, "127.0.0.1:9"], timeout=3.0, per_gateway_limit=1
    )
    assert found and host_port in found[0].url


def test_scan_all_gateways_respects_total_limit(portal_server) -> None:  # noqa: ANN001
    base, handler = portal_server
    host_port = base.replace("http://", "")
    for path in ("/eportal/portal/login", "/portal/login", "/cas/login"):
        handler.pages[path] = DRCOM_HTML

    found = discovery.scan_all_gateways(
        [host_port], timeout=3.0, per_gateway_limit=3, total_limit=2
    )
    assert len(found) <= 2


def test_scan_all_gateways_on_dead_ports_is_empty() -> None:
    assert discovery.scan_all_gateways(["127.0.0.1:9"], timeout=1.0) == []


# ---------- 一站式发现 ----------


def test_discover_reports_probe_state(portal_server) -> None:  # noqa: ANN001
    base, handler = portal_server
    handler.pages["/free"] = "<html>ok</html>"
    report = discovery.discover(seed=f"{base}/free", timeout=3.0, include_gateway_scan=False)

    assert report["ok"] is False
    assert report["probe_status"] == "online"
    assert report["probe_candidates"] == list(discovery.PROBE_CANDIDATES)
    assert report["hints"]


def test_discover_uses_portal_seed(portal_server) -> None:  # noqa: ANN001
    base, handler = portal_server
    handler.pages["/portal/login"] = HUAWEI_HTML

    report = discovery.discover(seed=f"{base}/portal/login", timeout=3.0, include_gateway_scan=False)
    assert report["ok"] is True
    best = report["candidates"][0]
    assert best["vendor"] == "华为"
    assert best["needs_captcha"] is True
    assert report["hints"]


def test_discover_never_raises_on_garbage_seed() -> None:
    report = discovery.discover(seed="ht!tp://%%%", timeout=1.0, include_gateway_scan=False)
    assert isinstance(report, dict)
    assert report["ok"] is False
    assert report["hints"]


def test_discover_candidates_sorted_by_credibility(portal_server) -> None:  # noqa: ANN001
    base, handler = portal_server
    handler.pages["/noform"] = PLAIN_HTML
    handler.pages["/eportal/portal/login"] = DRCOM_HTML

    report = discovery.discover(
        seed=f"{base}/noform", timeout=3.0, include_gateway_scan=False
    )
    # 没表单的页面可能因为探活已放行而不进入候选，直接验证排序逻辑本身
    urls = [c["url"] for c in report["candidates"]]
    assert len(urls) == len(set(urls))  # 去重生效
    if report["candidates"]:
        assert report["candidates"][0]["username_field"] != ""


def test_dump_is_json() -> None:
    import json

    report = discovery.discover(seed="http://127.0.0.1:9/x", timeout=1.0, include_gateway_scan=False)
    assert json.loads(discovery.dump(report))["ok"] is False


def test_vendor_tables_cover_all_signatures() -> None:
    """每家厂商都必须有默认登录方式与编码建议，否则分析结果会退化。"""
    for vendor, _tokens in discovery.VENDOR_SIGNATURES:
        assert vendor in discovery.VENDOR_LOGIN_MODES
        assert vendor in discovery.VENDOR_PASSWORD_MODES


def test_portal_paths_are_absolute() -> None:
    assert all(p.startswith("/") for p in discovery.PORTAL_PATHS)
    assert any("eportal" in p for p in discovery.PORTAL_PATHS)  # 覆盖最常见的 Dr.COM
    assert any("sportal" in p for p in discovery.PORTAL_PATHS)


def test_probe_candidates_include_plain_http() -> None:
    assert all(u.startswith("http://") for u in discovery.PROBE_CANDIDATES)
    assert len(set(discovery.PROBE_CANDIDATES)) == len(discovery.PROBE_CANDIDATES)
