"""门户规则层：加载、校验、匹配顺序、用户覆盖与 CLI。"""

from __future__ import annotations

import json

import pytest

from campusnet import cli as cli_mod
from campusnet import discovery
from campusnet import profiles as profiles_mod


def test_builtin_profiles_are_valid() -> None:
    items, errors = profiles_mod.validate_all()

    assert errors == []
    ids = [item.id for item in items]
    assert ids[0] == "srun"
    assert "h3c-newstart" in ids
    assert ids[-1] == "webauth"  # 兜底规则排在最后


def test_identify_respects_order() -> None:
    """同时命中多条时按顺序取第一条：srun 必须赢过 h3c。"""
    profile, token = profiles_mod.identify(
        "<html>h3c srun_bx1</html>", "http://x/srun_portal_pc"
    )

    assert profile is not None
    assert profile.id == "srun"
    assert token


def test_identify_unknown_returns_none() -> None:
    assert profiles_mod.identify("<html>nothing</html>", "http://x/") == (None, "")


def test_user_dir_overrides_builtin(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    custom = tmp_path / "profiles"
    custom.mkdir()
    (custom / "01-srun.json").write_text(
        json.dumps(
            {
                "id": "srun",
                "name": "本校 SRUN",
                "match_url": ["srun_portal"],
                "login_mode": "srun",
                "password_mode": "plain",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(profiles_mod.USER_DIR_ENV, str(custom))

    items = profiles_mod.load_profiles()

    assert items[0].name == "本校 SRUN"
    assert items[0].source == "01-srun.json"
    assert len([item for item in items if item.id == "srun"]) == 1


def test_invalid_login_mode_is_rejected(tmp_path) -> None:  # noqa: ANN001
    bad = tmp_path / "profiles"
    bad.mkdir()
    (bad / "x.json").write_text(
        json.dumps({"id": "x", "name": "X", "login_mode": "teleport"}), encoding="utf-8"
    )

    with pytest.raises(profiles_mod.ProfileError, match="login_mode"):
        profiles_mod.load_profiles(bad)


def test_missing_id_is_rejected(tmp_path) -> None:  # noqa: ANN001
    bad = tmp_path / "profiles"
    bad.mkdir()
    (bad / "y.json").write_text(json.dumps({"name": "只有名字"}), encoding="utf-8")

    with pytest.raises(profiles_mod.ProfileError, match="id 与 name"):
        profiles_mod.load_profiles(bad)


def test_broken_json_is_rejected(tmp_path) -> None:  # noqa: ANN001
    bad = tmp_path / "profiles"
    bad.mkdir()
    (bad / "z.json").write_text("{不是 json", encoding="utf-8")

    with pytest.raises(profiles_mod.ProfileError, match="读取失败"):
        profiles_mod.load_profiles(bad)


def test_discovery_tables_derive_from_profiles() -> None:
    """旧的三张表必须与规则文件一致，避免两处漂移。"""
    names = [item.name for item in profiles_mod.load_profiles()]

    assert [vendor for vendor, _ in discovery.VENDOR_SIGNATURES] == names
    for vendor in names:
        assert vendor in discovery.VENDOR_LOGIN_MODES
        assert vendor in discovery.VENDOR_PASSWORD_MODES


def test_discovery_identify_vendor_uses_profiles() -> None:
    vendor, token = discovery.identify_vendor("<html>srun_bx1</html>", "http://x/")

    assert vendor == "深澜 SRUN"
    assert token


def test_cli_profiles_validate_ok(capsys) -> None:  # noqa: ANN001
    assert cli_mod.main(["profiles", "--validate"]) == 0

    assert "srun" in capsys.readouterr().out


def test_cli_profiles_lists_rules(capsys) -> None:  # noqa: ANN001
    assert cli_mod.main(["profiles"]) == 0

    assert "深澜 SRUN" in capsys.readouterr().out


DECOY_PAGE = """
<html><body>
<form action="/search">
  <input name="kw" type="text">
  <input name="pwd" type="password">
  <input name="extra1" type="text">
  <input name="extra2" type="text">
</form>
<form action="/eportal/portal/login">
  <input name="DDDDD" type="text">
  <input name="upass" type="password">
  <input name="0MKKey" type="hidden">
</form>
</body></html>
"""


def test_pick_login_form_prefers_profile_fields() -> None:
    """页面里有两个都像登录的表单时，规则里写明的字段名优先。"""
    from campusnet import portal

    profile, _ = profiles_mod.identify(DECOY_PAGE, "http://10.1.2.3/eportal/portal/login")
    assert profile is not None and profile.id == "drcom"
    forms = portal.parse_forms(DECOY_PAGE)

    heuristic = portal.pick_login_form(forms)
    with_profile = portal.pick_login_form(forms, profile)

    # 光靠启发式会挑字段更多、也带 password 的那个搜索表单
    assert heuristic is not None and "pwd" in heuristic.fields
    assert with_profile is not None
    assert "DDDDD" in with_profile.fields
    assert "upass" in with_profile.fields


def test_field_from_profile_maps_candidates() -> None:
    from campusnet import portal

    profile, _ = profiles_mod.identify("<html>srun_bx1</html>", "http://x/")
    assert profile is not None
    fields = {"DDDDD": "", "upass": "", "0MKKey": ""}

    assert (
        portal._field_from_profile(profile, fields, profile.username_fields) == "DDDDD"
    )
    assert portal._field_from_profile(profile, fields, profile.password_fields) == "upass"
    assert portal._field_from_profile(None, fields, ("x",)) == ""
    assert portal._field_from_profile(profile, fields, ("missing",)) == ""
