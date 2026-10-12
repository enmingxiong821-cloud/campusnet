"""冻结（单文件 exe）模式：资源与数据分家、启动命令用 exe 自身。"""

from __future__ import annotations

from pathlib import Path

from campusnet import bootstart, paths


def test_dev_mode_roots_are_the_repo() -> None:
    resource, data = paths.resolve_roots(
        frozen=False, bundle=Path("/bundle"), source=Path("/repo")
    )

    assert resource == Path("/repo")
    assert data == Path("/repo")


def test_frozen_mode_splits_resource_and_data(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """冻结后资源在解包目录（只读、易失），数据必须落到用户目录。"""
    monkeypatch.delenv("CAMPUSNET_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    home = tmp_path / "home"

    resource, data = paths.resolve_roots(
        frozen=True,
        bundle=Path("/bundle"),
        source=Path("/repo"),
        home=home,
    )

    assert resource == Path("/bundle")
    assert data.name == "campusnet"
    # 关键不变量（跨平台）：数据不能落在解包目录里，否则单文件每次运行都会丢配置。
    assert Path("/bundle") not in data.parents
    assert (home in data.parents) or (tmp_path / "Local" in data.parents)


def test_home_override_wins(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    target = tmp_path / "custom"
    monkeypatch.setenv("CAMPUSNET_HOME", str(target))

    _, data = paths.resolve_roots(
        frozen=True, bundle=Path("/bundle"), source=Path("/repo")
    )

    assert data == target


def test_schema_lives_with_resources_and_db_with_data() -> None:
    """建表脚本是只读资源，数据库是可写数据——两者不能混。"""
    assert paths.SCHEMA_PATH.parent == paths.RESOURCE_ROOT / "database"
    assert paths.DATABASE_DIR == paths.ROOT / "database"


def test_frozen_launch_command_uses_the_exe(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(bootstart, "frozen_prefix", lambda: ["C:/app/campusnet.exe"])

    command = bootstart.launch_command(port=2268)
    line = bootstart.launch_command_line(["check"])

    assert command[0] == "C:/app/campusnet.exe"
    assert "serve" in command
    assert "--port" in command
    assert line.startswith("C:/app/campusnet.exe")
    assert "run.py" not in line


def test_dev_launch_command_uses_run_script(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(bootstart, "frozen_prefix", lambda: None)

    line = bootstart.launch_command_line(["check"])

    assert "run.py" in line
    assert "check" in line
