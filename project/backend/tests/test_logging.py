"""运行日志：按天分文件、保留策略、print 双写与幂等。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest

from campusnet import logging_setup


@pytest.fixture(autouse=True)
def clean_logging():
    yield
    logging_setup.teardown_logging()


def test_print_output_lands_in_log_file(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("CAMPUSNET_LOG_DIR", str(tmp_path))
    target = logging_setup.setup_logging()

    assert target is not None
    assert target.parent == tmp_path
    print("无人值守可观测性")

    assert "无人值守可观测性" in target.read_text(encoding="utf-8")


def test_logger_messages_land_in_log_file(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("CAMPUSNET_LOG_DIR", str(tmp_path))
    logging_setup.setup_logging()

    logging.getLogger("campusnet.test").warning("探活未放行")

    text = logging_setup.log_path().read_text(encoding="utf-8")
    assert "探活未放行" in text


def test_setup_is_idempotent(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """重复调用不能叠加 handler，否则同一条日志会被写两遍。"""
    monkeypatch.setenv("CAMPUSNET_LOG_DIR", str(tmp_path))
    first = logging_setup.setup_logging()
    second = logging_setup.setup_logging()

    assert first == second
    handlers = [
        item
        for item in logging.getLogger().handlers
        if isinstance(item, logging_setup._SinkHandler)
    ]
    assert len(handlers) == 1


def test_prune_removes_expired_logs(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("CAMPUSNET_LOG_DIR", str(tmp_path))
    stamp = lambda days: (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    expired = tmp_path / f"campusnet-{stamp(30)}.log"
    kept = tmp_path / f"campusnet-{stamp(1)}.log"
    expired.write_text("old", encoding="utf-8")
    kept.write_text("keep", encoding="utf-8")

    logging_setup.setup_logging(keep_days=7)

    assert not expired.exists()
    assert kept.exists()


def test_unwritable_log_dir_falls_back(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """目录不可写时返回 None 并继续运行，而不是让服务起不来。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setenv("CAMPUSNET_LOG_DIR", str(blocker))

    assert logging_setup.setup_logging() is None


def test_teardown_restores_streams(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("CAMPUSNET_LOG_DIR", str(tmp_path))
    original = logging_setup.sys.stdout
    logging_setup.setup_logging()
    assert isinstance(logging_setup.sys.stdout, logging_setup._TeeStream)

    logging_setup.teardown_logging()

    assert logging_setup.sys.stdout is original
