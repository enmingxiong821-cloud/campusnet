"""HTTPS 上下文收敛后的行为测试。

四处调用点（探活 / 门户 / 深澜 SRUN / 门户发现）现在共用
`tls.portal_context`，所以这里两头都要测：构造本身对不对，
以及各调用方确实把它用上了——否则「收敛」只是把重复挪了个地方，
下次还是可能有人另起一份。
"""

from __future__ import annotations

import ssl
import urllib.request
from http.cookiejar import CookieJar

from campusnet import discovery as discovery_mod
from campusnet import portal as portal_mod
from campusnet import powerprep as powerprep_mod
from campusnet import probe as probe_mod
from campusnet import srun as srun_mod
from campusnet import tls as tls_mod


def _https_context(opener: urllib.request.OpenerDirector) -> ssl.SSLContext:
    """从 opener 里取出 HTTPSHandler 手上的上下文。

    `_context` 是私有属性，但标准库没给公开读法，且这个字段从 3.x 一路没变过。
    """
    handler = next(
        h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)
    )
    return handler._context  # type: ignore[attr-defined]


# ---------------------------------------------------------------- 构造本身


def test_default_context_keeps_verification_on() -> None:
    """不传 insecure 时保持标准校验，别顺手把所有人的证书校验都关了。"""
    context = tls_mod.portal_context()
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_insecure_context_skips_verification() -> None:
    """insecure=True 要真的关掉校验，且两个属性都关（只关一个会抛 ValueError）。"""
    context = tls_mod.portal_context(insecure=True)
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_NONE


def test_insecure_keyword_call_is_accepted() -> None:
    """按关键字传参也要能跑——调用点普遍写成 portal_context(insecure=...)。"""
    assert tls_mod.portal_context(insecure=False).verify_mode == ssl.CERT_REQUIRED


# ---------------------------------------------------------------- 调用方接上了


def test_probe_opener_follows_insecure_flag() -> None:
    """探活：insecure 为真/假时上下文要跟着变，不能写死。"""
    assert _https_context(probe_mod._build_opener(True)).verify_mode == ssl.CERT_NONE
    assert (
        _https_context(probe_mod._build_opener(False)).verify_mode == ssl.CERT_REQUIRED
    )


def test_portal_opener_follows_insecure_flag() -> None:
    """门户登录：默认不关校验，显式要求时才关。"""
    assert (
        _https_context(portal_mod._opener(CookieJar())).verify_mode == ssl.CERT_REQUIRED
    )
    assert (
        _https_context(portal_mod._opener(CookieJar(), insecure=True)).verify_mode
        == ssl.CERT_NONE
    )


def test_srun_opener_follows_insecure_flag() -> None:
    """深澜 SRUN：同上。"""
    assert _https_context(srun_mod._opener(True)).verify_mode == ssl.CERT_NONE
    assert _https_context(srun_mod._opener(False)).verify_mode == ssl.CERT_REQUIRED


def test_discovery_opener_is_always_insecure() -> None:
    """门户发现扫的是网关与内网地址，自签证书是常态，无条件关校验。"""
    assert _https_context(discovery_mod._fetch_opener()).verify_mode == ssl.CERT_NONE


def test_powerprep_time_check_uses_insecure_context(monkeypatch) -> None:  # noqa: ANN001
    """电源体检的时间比对：时钟本身是被测对象，必须关校验，否则永远测不出偏差。"""
    captured: list[object] = []

    class _Resp:
        headers: dict[str, str] = {"Date": "Wed, 01 Jan 2025 00:00:00 GMT"}

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def fake_urlopen(request, timeout=None, context=None):  # noqa: ANN001, ANN202
        captured.append(context)
        return _Resp()

    monkeypatch.setattr(powerprep_mod.urllib.request, "urlopen", fake_urlopen)
    # 本机时区未知，比对结果不重要，这里只看上下文有没有按 insecure 传下去
    powerprep_mod._check_time_skew()

    assert captured, "时间比对没有发出请求"
    context = captured[0]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_NONE
