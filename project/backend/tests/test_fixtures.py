"""门户 fixture：脱敏、往返与离线回放。"""

from __future__ import annotations

from campusnet import cli as cli_mod
from campusnet import fixtures as fixtures_mod

SAMPLE = """
<html><head><title>认证</title>
<script>var token = "deadbeefdeadbeefdeadbeef";</script>
<style>.a { color: red }</style></head>
<body>
<!-- 内部备注 -->
<form name="login" method="post" action="/eportal/portal/login?userip=10.1.2.3&token=abc">
  <input type="text" name="DDDDD" id="DDDDD" value="2012345678" placeholder="学号">
  <input type="password" name="upass" value="secret">
  <input type="hidden" name="0MKKey" value="1234abcd5678ef90">
</form>
<p>联系 13800000000</p>
</body></html>
"""


def test_sanitize_drops_values_and_scripts() -> None:
    clean = fixtures_mod.sanitize_html(SAMPLE)

    assert "2012345678" not in clean  # 学号
    assert "secret" not in clean  # 密码值
    assert "13800000000" not in clean  # 手机号
    assert "deadbeef" not in clean  # 脚本正文
    assert "color: red" not in clean  # 样式正文
    assert "内部备注" not in clean  # 注释
    assert "token=abc" not in clean  # URL 查询串
    assert 'name="DDDDD"' in clean  # 结构仍在
    assert 'name="0MKKey"' in clean
    assert "<form" in clean and "</form>" in clean


def test_sanitize_marks_identifiers() -> None:
    clean = fixtures_mod.sanitize_html(
        "<p>学号 2012345678 手机 13800000000 网关 10.1.2.3</p>"
    )

    assert "<学号>" in clean
    assert "<手机号>" in clean
    assert "<内网地址>" in clean


def test_fixture_round_trip_and_replay(tmp_path) -> None:  # noqa: ANN001
    path = fixtures_mod.write_fixture(
        fixtures_mod.Fixture(
            html=fixtures_mod.sanitize_html(SAMPLE),
            url="http://10.1.2.3/eportal/portal/login",
        ),
        tmp_path / "portal.html",
    )

    info = fixtures_mod.replay(path)

    assert info["vendor"] == "Dr.COM"
    assert info["username_field"] == "DDDDD"
    assert info["password_field"] == "upass"
    assert "0MKKey" in info["hidden_fields"]  # type: ignore[operator]


def test_read_fixture_tolerates_missing_header(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "plain.html"
    path.write_text("<html><body>x</body></html>", encoding="utf-8")

    fixture = fixtures_mod.read_fixture(path)

    assert fixture.url == ""
    assert "x" in fixture.html


def test_cli_replay_prints_fields(tmp_path, capsys) -> None:  # noqa: ANN001
    path = fixtures_mod.write_fixture(
        fixtures_mod.Fixture(
            html=fixtures_mod.sanitize_html(SAMPLE),
            url="http://10.1.2.3/eportal/portal/login",
        ),
        tmp_path / "portal.html",
    )

    assert cli_mod.main(["portal-report", "--replay", str(path)]) == 0

    out = capsys.readouterr().out
    assert "DDDDD" in out
    assert "Dr.COM" in out


def test_cli_portal_report_from_file(tmp_path, capsys) -> None:  # noqa: ANN001
    source = tmp_path / "saved.html"
    source.write_text(SAMPLE, encoding="utf-8")
    out_path = tmp_path / "fixture.html"

    code = cli_mod.main(
        [
            "portal-report",
            "--from-file",
            str(source),
            "--url",
            "http://10.1.2.3/eportal/portal/login",
            "--out",
            str(out_path),
        ]
    )

    assert code == 0
    text = out_path.read_text(encoding="utf-8")
    assert "2012345678" not in text
    printed = capsys.readouterr().out
    assert "脱敏 fixture" in printed
    assert "Dr.COM" in printed


def test_cli_portal_report_requires_source(capsys) -> None:  # noqa: ANN001
    assert cli_mod.main(["portal-report"]) == 2

    assert "请给出 --url" in capsys.readouterr().err


def test_replay_bundled_fixture() -> None:
    """投稿来的 fixture 固化在 tests/fixtures 下，回放一次防识别退化。"""
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "drcom-sample.html"

    info = fixtures_mod.replay(path)

    assert info["vendor"] == "Dr.COM"
    assert info["username_field"] == "DDDDD"
    assert info["password_field"] == "upass"
    assert "0MKKey" in info["hidden_fields"]  # type: ignore[operator]
