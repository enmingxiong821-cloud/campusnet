"""单实例锁：防止两个服务同时操作同一份 SQLite。

监控与事件写入是有状态的，多进程并存会互相覆盖 last_state、
重复触发登录，以及放大 SQLite 锁竞争。这里用 PID 文件做互斥。
"""

from __future__ import annotations

import os
from pathlib import Path

# Linux 默认 pid_max=4194304（2^22），这里放宽到 2^31 覆盖所有现实配置。
# 再大的值 os.kill 会抛 OverflowError，直接判死即可。
_MAX_PID = 1 << 31


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    # Linux 的 PID 空间受 /proc/sys/kernel/pid_max 限制，
    # 常见上限 4194304；超出范围的 PID 直接判死，不去问内核。
    # 不加这道闸，os.kill(1<<40, 0) 会抛 OverflowError，
    # 而 OverflowError 不是 OSError 子类，会穿透下面的 except 逃出去，
    # 让整个 acquire() 崩掉——锁文件被写坏就能让服务起不来。
    if pid > _MAX_PID:
        return False
    if os.name == "nt":
        # Windows 没有 os.kill(0)，用 ctypes 查 OpenProcess 退出码
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不是我们开的
    except (OSError, OverflowError, ValueError):
        return False
    return True


class SingleInstance:
    """基于 PID 文件的互斥锁。进程异常退出后残留的锁会被自动接管。

    注意：PID 文件只能区分「进程」，无法区分「同一进程里的两个实例」。
    同进程重复 acquire 会返回 True（拿到的是同一把锁），
    跨进程才是真正互斥的那一层。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.acquired = False

    def holder_pid(self) -> int | None:
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        try:
            pid = int(text.split()[0])
        except (ValueError, IndexError):
            return None
        return pid if pid > 0 else None

    def is_held_by_other_process(self) -> bool:
        """锁文件存在且持有者是另一个活着的进程。"""
        pid = self.holder_pid()
        return bool(pid and pid != os.getpid() and _pid_alive(pid))

    def acquire(self) -> bool:
        if self.acquired:
            return True

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self.is_held_by_other_process():
                return False
            # 残留锁（上次崩溃）或同进程旧锁，接管
            self._safe_unlink()
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileExistsError, OSError):
                return False
        except OSError:
            return False

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"{os.getpid()}\n")
        except OSError:
            self._safe_unlink()
            return False

        self.acquired = True
        return True

    def release(self) -> None:
        if self.acquired:
            self._safe_unlink()
            self.acquired = False

    def _safe_unlink(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "SingleInstance":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()