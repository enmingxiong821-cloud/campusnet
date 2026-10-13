"""运行日志：按天分文件、保留策略、print 双写与幂等。"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

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


# ---------------------------------------------- 降级与容错分支（F2）
# 日志模块的每一处 try/except 都是「出问题时别把服务带崩」的兜底，
# 平时跑不到，正是最需要钉住的那些行。


def _boom(*_args, **_kwargs):  # noqa: ANN202
    raise OSError("模拟系统调用失败")


def test_date_from_name_rejects_foreign_names() -> None:
    """别人的日志文件不能被当成我们的历史日志删掉。"""
    assert logging_setup._date_from_name("campusnet-2026-10-13.txt") is None
    assert logging_setup._date_from_name("other-2026-10-13.log") is None
    assert logging_setup._date_from_name("campusnet-不是日期.log") is None
    assert logging_setup._date_from_name("campusnet-2026-10-13.log") == date(
        2026, 10, 13
    )


def test_daily_sink_write_without_stream_is_noop(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """拿不到句柄时静默丢弃：日志写不进去不该让主流程抛异常。"""
    sink = logging_setup._DailySink(tmp_path, 7)
    monkeypatch.setattr(sink, "_stream_for_today", lambda: None)

    sink.write("丢弃")

    assert not list(tmp_path.glob("*.log"))


def test_daily_sink_close_swallows_error(tmp_path) -> None:  # noqa: ANN001
    sink = logging_setup._DailySink(tmp_path, 7)

    class BoomStream:
        def close(self) -> None:
            raise OSError("句柄已经没了")

    sink._stream = BoomStream()
    sink.close()  # 不该抛

    assert sink._stream is None


def test_prune_skips_unremovable_file(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """删不掉的历史日志跳过就好，不能因此让日志系统起不来。"""
    expired = tmp_path / (
        "campusnet-"
        + (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        + ".log"
    )
    expired.write_text("old", encoding="utf-8")
    monkeypatch.setattr(Path, "unlink", _boom)

    assert logging_setup._DailySink(tmp_path, 7).prune() == []
    assert expired.exists()


def test_sink_handler_routes_write_failure_to_handle_error(capsys) -> None:  # noqa: ANN001
    class BadSink:
        def write(self, text: str) -> None:
            raise RuntimeError("磁盘满了")

    handler = logging_setup._SinkHandler(BadSink())  # type: ignore[arg-type]

    handler.emit(logging.makeLogRecord({"msg": "x"}))  # 不该抛

    assert "磁盘满了" in capsys.readouterr().err


def test_null_stream_absorbs_writes() -> None:
    """pythonw 下 sys.stdout 是 None，print 需要一个可写的空靶子。"""
    stream = logging_setup._NullStream()

    assert stream.write("abc") == 3
    assert stream.flush() is None
    assert stream.isatty() is False
    assert stream.encoding == "utf-8"


def test_tee_stream_falls_back_to_null_stream(tmp_path) -> None:  # noqa: ANN001
    tee = logging_setup._TeeStream(None, logging_setup._DailySink(tmp_path, 7))

    assert isinstance(tee._stream, logging_setup._NullStream)


def test_tee_stream_swallows_target_failures(tmp_path) -> None:  # noqa: ANN001
    """终端写失败（管道被关）不能连累日志文件那份。"""
    sink = logging_setup._DailySink(tmp_path, 7)

    class Boom:
        encoding = "utf-8"

        def write(self, text: str) -> None:
            raise OSError("管道已关闭")

        def flush(self) -> None:
            raise OSError("管道已关闭")

    tee = logging_setup._TeeStream(Boom(), sink)

    assert tee.write("还活着") == 3
    tee.flush()  # 两边都失败也不该抛

    assert "还活着" in sink.current_path().read_text(encoding="utf-8")


def test_tee_stream_delegates_terminal_metadata(tmp_path) -> None:  # noqa: ANN001
    class Inner:
        encoding = "cp936"

        def __init__(self) -> None:
            self.seen: dict = {}

        def write(self, text: str) -> int:
            return len(text)

        def flush(self) -> None:
            return None

        def fileno(self) -> int:
            return 7

        def reconfigure(self, **kwargs) -> None:  # noqa: ANN003
            self.seen = kwargs

    inner = Inner()
    tee = logging_setup._TeeStream(inner, logging_setup._DailySink(tmp_path, 7))

    assert tee.isatty() is False  # 双写流对外一律不是终端
    assert tee.fileno() == 7
    assert tee.encoding == "cp936"

    tee.reconfigure(encoding="utf-8", errors="replace")
    assert inner.seen == {"encoding": "utf-8", "errors": "replace"}


def test_tee_stream_reconfigure_without_support(tmp_path) -> None:  # noqa: ANN001
    """底层流没有 reconfigure（如 _NullStream）时安静跳过。"""
    tee = logging_setup._TeeStream(
        logging_setup._NullStream(), logging_setup._DailySink(tmp_path, 7)
    )

    assert tee.reconfigure(encoding="utf-8") is None
    assert tee.encoding == "utf-8"


def test_tee_stream_reconfigure_swallows_errors(tmp_path) -> None:  # noqa: ANN001
    class Grumpy:
        def reconfigure(self, **kwargs) -> None:  # noqa: ANN003
            raise ValueError("不支持的参数")

    tee = logging_setup._TeeStream(Grumpy(), logging_setup._DailySink(tmp_path, 7))

    tee.reconfigure(encoding="utf-8")  # 不该抛


def test_install_tee_is_idempotent(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """已经装过就别再包一层，否则每装一次日志被写多一遍。"""
    monkeypatch.setenv("CAMPUSNET_LOG_DIR", str(tmp_path))
    logging_setup.setup_logging()
    first = logging_setup.sys.stdout

    logging_setup._install_tee(logging_setup._sink)  # type: ignore[arg-type]

    assert logging_setup.sys.stdout is first


def test_install_tee_survives_failure(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """拿不到标准流时放弃双写，不能让启动流程挂掉。"""

    class BoomTee(logging_setup._TeeStream):
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("拿不到标准流")

    monkeypatch.setattr(logging_setup, "_TeeStream", BoomTee)

    logging_setup._install_tee(logging_setup._DailySink(tmp_path, 7))  # 不该抛


def test_setup_reinstalls_when_path_changes(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """日志目录改了要换一份，旧的 handler 必须先卸掉，否则两份都在写。"""
    monkeypatch.setenv("CAMPUSNET_LOG_DIR", str(tmp_path / "a"))
    logging_setup.setup_logging()
    stale = logging_setup.sys.stdout

    monkeypatch.setenv("CAMPUSNET_LOG_DIR", str(tmp_path / "b"))
    target = logging_setup.setup_logging()

    assert target is not None
    assert target.parent == tmp_path / "b"
    assert logging_setup.sys.stdout is not stale
    handlers = [
        item
        for item in logging.getLogger().handlers
        if isinstance(item, logging_setup._SinkHandler)
    ]
    assert len(handlers) == 1
