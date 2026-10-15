"""门户登录逻辑单元测试：表单解析、字段识别、响应分类。"""

from __future__ import annotations

import pytest

from campusnet.portal import (
    LoginResult,
    classify_response,
    parse_forms,
    pick_login_form,
)

DRCOM_PAGE = """
<html><body>
<form name="loginForm" action="/eportal/portal/login" method="post">
  <input type="hidden" name="user_ip" value="10.0.0.5">
  <input type="hidden" name="hiddenPwd" value="">
  <input type="text" name="DDDDD" value="">
  <input type="password" name="upass" value="">
  <input type="hidden" name="0MKKey" value="">
  <input type="submit" name="submitBtn" value="登录">
</form>
</body></html>
"""

CAS_PAGE = """
<html><body>
<form action="/cas/login" method="GET">
  <input name="username" type="text">
  <input name="password" type="password">
  <button type="submit">Sign in</button>
</form>
</body></html>
"""

NO_FORM_PAGE = "<html><body><div>请使用手机客户端认证</div></body></html>"


def test_parse_forms_reads_action_method_and_fields() -> None:
    forms = parse_forms(DRCOM_PAGE)
    assert len(forms) == 1
    form = forms[0]
    assert form.action == "/eportal/portal/login"
    assert form.method == "POST"
    assert form.fields["DDDDD"] == ""
    assert form.fields["upass"] == ""
    assert form.fields["user_ip"] == "10.0.0.5"
    assert form.submit_field == "submitBtn"


def test_submit_input_not_in_payload() -> None:
    form = parse_forms(DRCOM_PAGE)[0]
    assert "submitBtn" not in form.fields


def test_button_submitted_field_ignored_but_kept_as_reference() -> None:
    form = parse_forms(CAS_PAGE)[0]
    assert form.method == "GET"
    assert set(form.fields) == {"username", "password"}


def test_classify_detects_username_and_password_fields() -> None:
    form = pick_login_form(parse_forms(DRCOM_PAGE))
    assert form is not None
    assert form.username_field == "DDDDD"
    assert form.password_field == "upass"


def test_classify_handles_generic_cas_fields() -> None:
    form = pick_login_form(parse_forms(CAS_PAGE))
    assert form is not None
    assert form.username_field == "username"
    assert form.password_field == "password"


def test_classify_picks_form_with_password_when_several() -> None:
    page = """
    <form action="/search" method="get"><input name="q" type="text"></form>
    <form action="/login" method="post">
      <input name="user" type="text"><input name="pwd" type="password">
    </form>
    """
    form = pick_login_form(parse_forms(page))
    assert form is not None
    assert form.action == "/login"


def test_no_form_returns_none() -> None:
    assert pick_login_form(parse_forms(NO_FORM_PAGE)) is None


def test_unquoted_attributes_are_parsed() -> None:
    page = "<form action=/login method=post><input name=user type=text><input name=pwd type=password>"
    form = pick_login_form(parse_forms(page))
    assert form is not None
    assert form.action == "/login"
    assert form.password_field == "pwd"


def test_html_entities_in_values_are_decoded() -> None:
    page = '<form action="/l"><input name="u" value="a&amp;b" type="text"><input name="p" type="password">'
    form = pick_login_form(parse_forms(page))
    assert form is not None
    assert form.fields["u"] == "a&b"


# ---------- 响应分类 ----------


def test_classify_result_1_is_success() -> None:
    r = classify_response('{"result":1}', 200, "http://10.0.0.1/ok")
    assert r.state == "success" and r.ok


def test_classify_result_eq_1_is_success() -> None:
    """等号写法（有的门户回 result=1 而不是 JSON）也要认。"""
    r = classify_response("result=1&msg=ok", 200, "http://10.0.0.1/ok")
    assert r.state == "success" and r.ok


@pytest.mark.parametrize(
    "body",
    [
        '{"result":10}',
        '{"result":12}',
        "result=10",
        "result=12",
        "result=-1",
        "result=0",
        "result = 10",
    ],
)
def test_classify_result_not_one_is_failed(body: str) -> None:
    """result 不是 1 一律算失败。

    两种写法都要卡边界。不卡的时候 `result=10` 既命中不了成功判定，也不带任何
    失败标记，会掉进末尾那条宽松兜底分支被判成功——那次误判会让「根本没连上」
    显示成「已连接」。
    """
    r = classify_response(body, 200, "http://10.0.0.1/ok")
    assert r.state == "failed" and not r.ok


def test_classify_result_boundary_rejects_prefixed_field() -> None:
    """`myresult=1` 不是 result 字段，不能靠裸子串命中。

    用门户地址，让末尾的宽松兜底不介入，从而看出它确实没被当成 result=1。
    """
    r = classify_response("myresult=1", 200, "http://10.0.0.1/eportal/portal/login")
    assert r.state == "failed"


def test_classify_success_true_flag() -> None:
    r = classify_response('{"success": true}', 200, "http://x/")
    assert r.state == "success"


def test_classify_result_0_is_failed() -> None:
    r = classify_response('{"result":0}', 200, "http://x/")
    assert r.state == "failed" and not r.ok


def test_classify_password_error() -> None:
    r = classify_response("<p>密码错误</p>", 200, "http://x/")
    assert r.state == "failed"
    assert "密码错误" in r.message
    assert r.terminal is True


def test_classify_password_error_is_terminal_but_login_page_is_not() -> None:
    """凭据被拒是终态（重试无用），停留在登录页是瞬态（可重试）。"""
    cred = classify_response("passworderror", 200, "http://x/")
    assert cred.terminal is True
    still = classify_response("<html>login</html>", 200, "http://10.0.0.1/eportal/portal/login")
    assert still.state == "failed"
    assert still.terminal is False


def test_classify_userdeny_is_terminal() -> None:
    r = classify_response('{"result":0,"userdeny":"1"}', 200, "http://x/")
    assert r.terminal is True


@pytest.mark.parametrize(
    "body",
    [
        "<div>您已经在线</div>",
        "<div>您已在线</div>",
        "<div>该账号已在线，请勿重复登录</div>",
        "<div>You are already online</div>",
    ],
)
def test_classify_already_online(body: str) -> None:
    """几种真实措辞都要认出来。

    「您已经在线」里没有连续的「已在线」三个字——原来那条裸的「在线」子串
    恰好蒙对了，换掉裸子串时必须把「已经在线」补上。
    """
    r = classify_response(body, 200, "http://x/")

    assert r.state == "already_online" and r.ok


def test_classify_credential_failure_wins_over_already_online() -> None:
    """明确的密码错误不能被「已在线」盖掉。

    `already_online` 的 `ok` 为真，且不带 terminal——被盖住的那次不触发
    600 秒冷却，程序就拿着错密码一轮轮重登（有锁号风险）。
    """
    r = classify_response(
        "<html>密码错误，请重试；如需帮助请点击在线客服</html>", 200, "http://x/"
    )

    assert r.state == "failed"
    assert r.terminal is True


def test_classify_bare_online_substring_is_not_already_online() -> None:
    """正文里带「在线」不等于「账号已在线」。

    门户页脚常写「在线客服」「当前在线人数」，登录页也可能带「在线时长」。
    原来这一条收的是裸的「在线」子串，这些页面全会被判成已在线。
    """
    for body in ("<div>当前在线人数：37</div>", "<a>在线客服</a>", "<span>在线时长 2:03</span>"):
        r = classify_response(body, 200, "http://10.0.0.1/eportal/portal/login")

        assert r.state == "failed", body


def test_classify_still_on_login_page_is_failure() -> None:
    r = classify_response("<html>login</html>", 200, "http://10.0.0.1/eportal/portal/login")
    assert r.state == "failed"
    assert "登录页" in r.message


def test_classify_unknown_status_is_error() -> None:
    r = classify_response("<html>boom</html>", 500, "http://10.0.0.1/x")
    assert r.state == "error"


def test_login_result_ok_flag() -> None:
    assert LoginResult("success", "").ok
    assert LoginResult("already_online", "").ok
    assert not LoginResult("failed", "").ok
    assert not LoginResult("error", "").ok
    assert not LoginResult("unknown", "").ok