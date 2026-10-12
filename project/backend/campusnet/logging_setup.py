"""无人值守运行日志：按天分文件、保留若干天，并把 print 输出一并落盘。

为什么单独一个模块：无人值守时 stdout 要么被自启动脚本重定向到文件
（Python 非终端时是块缓冲，可能长时间不落盘），要么根本没有终端
（pythonw / systemd journal）。出问题时用户唯一能依靠的证据就是文件日志，
所以「写到哪、留多久、什么时候 flush」必须在一处定死。

三条约束：

1. 只写仓库内的 database/logs/（可用 CAMPUSNET_LOG_DIR 覆盖），不碰系统目录；
2. 幂等：同一路径重复调用不叠加 handler，否则同一条日志会被写两遍；
3. 失败不致命：目录不可写时退回终端输出，不能因此让服务起不来。
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, TextIO

from .paths import DATABASE_DIR

DEFAULT_KEEP_DAYS = 7
_PREFIX = "campusnet-"
_SUFFIX = ".log"


def _filename_for(moment: datetime) -> str:
    return f"{_PREFIX}{moment.strftime('%Y-%m-%d')}{_SUFFIX}"


def _date_from_name(name: str) -> date | None:
    if not name.startswith(_PREFIX) or not name.endswith(_SUFFIX):
        return None
    stamp = name[len(_PREFIX) : -len(_SUFFIX)]
    try:
        return datetime.strptime(stamp, "%Y-%m-%d").date()
    except ValueError:
        return None


def log_dir() -> Path:
    override = os.environ.get("CAMPUSNET_LOG_DIR", "").strip()
    return Path(override).expanduser() if override else DATABASE_DIR / "logs"


def log_path(moment: datetime | None = None) -> Path:
    """日志文件路径；默认给今天的那一份，便于提示用户去哪看。"""
    return log_dir() / _filename_for(moment or datetime.now())


class _DailySink:
    """按天分文件的写入器，logging 与 print 共用同一个句柄。

    刻意不用 TimedRotatingFileHandler：它在 Windows 上轮转时要改名，
    而此时只要有第二个句柄打开同一文件，rename 就会失败。这里改成
    「按日期命名 + 跨天时重新打开」，全程只有一个句柄，两个平台都不冲突。
    """

    def __init__(self, directory: Path, keep_days: int) -> None:
        self.directory = directory
        self.keep_days = max(1, int(keep_days))
        self._day = ""
        self._stream: TextIO | None = None

    def current_path(self) -> Path:
        return self.directory / _filename_for(datetime.now())

    def write(self, text: str) -> None:
        stream = self._stream_for_today()
        if stream is None:
            return
        stream.write(text)
        # 无人值守下「写进缓冲区但没落盘」等于没写：每次落盘。
        stream.flush()

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def prune(self) -> list[Path]:
        """删掉超过保留天数的历史日志，返回被删文件。"""
        removed: list[Path] = []
        today = datetime.now().date()
        for item in self.directory.glob(f"{_PREFIX}*{_SUFFIX}"):
            stamp = _date_from_name(item.name)
            if stamp is None or (today - stamp).days > self.keep_days:
                try:
                    item.unlink()
                    removed.append(item)
                except OSError:
                    pass
        return removed

    def _stream_for_today(self) -> TextIO | None:
        day = datetime.now().strftime("%Y-%m-%d")
        if self._stream is not None and self._day == day:
            return self._stream
        self.close()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._stream = open(
            self.current_path(), "a", encoding="utf-8", buffering=1
        )
        self._day = day
        self.prune()
        return self._stream


class _SinkHandler(logging.Handler):
    def __init__(self, sink: _DailySink) -> None:
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.write(self.format(record) + "\n")
        except Exception:  # noqa: BLE001 - 日志失败绝不能影响主流程
            self.handleError(record)


class _NullStream:
    """pythonw 下 sys.stdout 是 None：给 print 一个可写的空目标。"""

    encoding = "utf-8"

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


class _TeeStream:
    """把 print 输出同时送到原始终端与日志文件。"""

    def __init__(self, stream: Any, sink: _DailySink) -> None:
        self._stream = stream if stream is not None else _NullStream()
        self._sink = sink

    def write(self, text: str) -> int:
        for target in (self._stream, self._sink):
            try:
                target.write(text)
            except Exception:  # noqa: BLE001 - 终端写失败不该中断程序
                pass
        return len(text)

    def flush(self) -> None:
        for target in (self._stream, self._sink):
            flush = getattr(target, "flush", None)
            if flush is None:
                continue
            try:
                flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._stream.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self._stream, "encoding", None) or "utf-8"

    def reconfigure(self, **kwargs: Any) -> None:
        reconfigure = getattr(self._stream, "reconfigure", None)
        if reconfigure is None:
            return
        try:
            reconfigure(**kwargs)
        except (OSError, ValueError):
            pass


_configured_path: Path | None = None
_sink: _DailySink | None = None
_handler: _SinkHandler | None = None


def setup_logging(
    level: int = logging.INFO, keep_days: int = DEFAULT_KEEP_DAYS
) -> Path | None:
    """安装文件日志并接管 stdout/stderr，返回今天的日志路径。

    重复调用是幂等的；路径变化（测试里常用 CAMPUSNET_LOG_DIR 改道）会重新安装。
    """
    global _configured_path, _sink, _handler
    target = log_path()
    if _configured_path == target and _sink is not None:
        return target
    if _configured_path is not None:
        teardown_logging()

    sink = _DailySink(target.parent, keep_days)
    try:
        sink.write("")
    except OSError as exc:
        print(
            f"[提示] 日志目录不可写（{target.parent}）：{exc}；本次仅输出到终端。",
            file=sys.stderr,
        )
        return None

    handler = _SinkHandler(sink)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    _sink, _handler, _configured_path = sink, handler, target
    _install_tee(sink)
    return target


def teardown_logging() -> None:
    """卸载 handler 与双写流；主要给测试用。"""
    global _configured_path, _sink, _handler
    if _handler is not None:
        logging.getLogger().removeHandler(_handler)
    if _sink is not None:
        _sink.close()
    _handler = _sink = None
    _configured_path = None
    if isinstance(sys.stdout, _TeeStream):
        sys.stdout = sys.stdout._stream  # type: ignore[assignment]
    if isinstance(sys.stderr, _TeeStream):
        sys.stderr = sys.stderr._stream  # type: ignore[assignment]


def _install_tee(sink: _DailySink) -> None:
    if isinstance(sys.stdout, _TeeStream) and isinstance(sys.stderr, _TeeStream):
        return
    try:
        if not isinstance(sys.stdout, _TeeStream):
            sys.stdout = _TeeStream(sys.stdout, sink)  # type: ignore[assignment]
        if not isinstance(sys.stderr, _TeeStream):
            sys.stderr = _TeeStream(sys.stderr, sink)  # type: ignore[assignment]
    except Exception:  # noqa: BLE001 - 拿不到标准流时不要影响启动
        pass
