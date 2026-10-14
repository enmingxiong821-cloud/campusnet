"""一键配置流程：环境检查、可选依赖、自检汇总与退出码。"""

from __future__ import annotations

import pytest

from campusnet import cli as cli_mod
from campusnet import doctor as doctor_mod
from campusnet import setup_flow


def _fake_doctor(**_kwargs: object) -> list[doctor_mod.Check]:
    return [
        doctor_mod.Check("运行环境", doctor_mod.OK, "ok"),
        doctor_mod.Check("账号", doctor_mod.WARN, "还没有账号"),
    ]


def test_python_check_blocks_old_version(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(setup_flow, "MIN_PYTHON", (99, 0))

    report = setup_flow.run_setup(make_shortcuts=False, check=False)

    assert report.ok is False
    assert any("需要 Python" in item for item in report.blockers)
    assert "✗" in report.render()


def test_setup_reports_shortcuts_and_doctor(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        setup_flow.shortcuts_mod,
        "create",
        lambda *a, **k: (True, "已创建 9 个快捷方式"),
    )
    monkeypatch.setattr(setup_flow.doctor_mod, "run", _fake_doctor)
    monkeypatch.setattr(setup_flow, "browser_ready", lambda: True)

    report = setup_flow.run_setup()

    text = report.render()
    assert "已创建 9 个快捷方式" in text
    assert "提示 1 项" in text
    assert "下一步" in text
    assert report.ok is True


def test_setup_blocks_on_doctor_failure(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(setup_flow.shortcuts_mod, "create", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(
        setup_flow.doctor_mod,
        "run",
        lambda **k: [doctor_mod.Check("数据库", doctor_mod.FAIL, "无法打开")],
    )

    report = setup_flow.run_setup()

    assert report.ok is False
    assert any("数据库" in item for item in report.blockers)


def test_shortcut_failure_is_not_blocking(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        setup_flow.shortcuts_mod, "create", lambda *a, **k: (False, "找不到 powershell")
    )
    monkeypatch.setattr(setup_flow.doctor_mod, "run", _fake_doctor)

    report = setup_flow.run_setup()

    assert report.ok is True
    assert "不影响使用" in report.render()


def test_browser_install_only_when_requested(monkeypatch) -> None:  # noqa: ANN001
    called: list[str] = []
    monkeypatch.setattr(setup_flow, "browser_ready", lambda: False)
    monkeypatch.setattr(
        setup_flow,
        "install_browser_deps",
        lambda: (called.append("x") or (True, "已安装")),
    )
    monkeypatch.setattr(setup_flow.shortcuts_mod, "create", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(setup_flow.doctor_mod, "run", _fake_doctor)

    setup_flow.run_setup()
    assert called == []

    setup_flow.run_setup(with_browser=True)
    assert called == ["x"]


def test_cli_setup_prints_report(monkeypatch, capsys) -> None:  # noqa: ANN001
    monkeypatch.setattr(setup_flow.doctor_mod, "run", _fake_doctor)

    code = cli_mod.main(["setup", "--no-shortcut"])

    out = capsys.readouterr().out
    assert code == 0
    assert "Python 环境" in out
    assert "下一步" in out


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("windows", "同意 UAC 并输入密码"),
        ("linux", "packaging/campusnet.service"),
        ("darwin", "com.campusnet.daemon.plist"),
    ],
)
def test_setup_next_step_matches_platform(
    monkeypatch,  # noqa: ANN001
    platform: str,
    expected: str,
) -> None:
    """下一步里的第一步，三个平台做的是不同的事。

    Windows 注册计划任务（要 UAC 与账户密码，换来「通电即认证」），另外两个平台
    只能注册用户级载体。原来两种平台共用一句话，等于给 Linux / macOS 用户承诺了
    「不需要任何人登录」——那正是这一轮要修的说法。
    """
    monkeypatch.setattr(setup_flow.doctor_mod, "run", _fake_doctor)
    monkeypatch.setenv("CAMPUSNET_FORCE_PLATFORM", platform)

    text = setup_flow.run_setup(make_shortcuts=False).render()

    assert expected in text
    if platform != "windows":
        assert "同意 UAC" not in text
