"""门户登录逻辑单元测试：表单解析、字段识别、响应分类。"""

from __future__ import annotations

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


def test_classify_already_online() -> None:
    r = classify_response("<div>您已经在线</div>", 200, "http://x/")
    assert r.state == "already_online" and r.ok


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