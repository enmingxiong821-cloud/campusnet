"""单实例锁：防止两个服务同时操作同一份 SQLite。

监控与事件写入是有状态的，多进程并存会互相覆盖 last_state、重复触发登录，
以及放大 SQLite 锁竞争。

互斥由**内核级文件锁**保证（POSIX `flock` / Windows `msvcrt.locking`）。
之前靠「读锁文件里的 PID，再判断那个进程还活着没有」，有两个挡不住的洞：

- **竞态**：从「判断持有者已死」到「删掉锁文件重建」之间，别的进程可能刚好建好了
  自己的锁文件；它会被我们删掉，于是两个进程都认为自己持锁。
- **PID 复用**：系统把同一个 PID 分给了无关进程，残留锁就被误判成「有人持有」，
  服务再也起不来（要靠人工删文件）。

文件锁由内核随进程退出自动释放——包括 `kill -9`、断电重启——上面两个洞都不存在。
锁文件里的 PID 从此只作**诊断展示**，不参与任何判定。
"""

from __future__ import annotations

import os
from pathlib import Path

# Linux 默认 pid_max=4194304（2^22），这里放宽到 2^31 覆盖所有现实配置。
# 再大的值 os.kill 会抛 OverflowError，直接判死即可。
_MAX_PID = 1 << 31

# 锁字节放在文件头 4KiB 处，PID 只写在前几个字节。
# 这么做的原因：Windows 的 msvcrt.locking 是**强制锁**——被锁的那段字节
# 连读都不允许。如果像 POSIX 那样锁整个文件，锁文件里的 PID 就永远读不出来，
# doctor 也就无法告诉用户「是谁占着」。
# 锁高位字节 + 只读文件头，两头都不碰对方。
_LOCK_BYTE_OFFSET = 4096
_PID_READ_LEN = 32


def pid_alive(pid: int) -> bool:
    """PID 对应的进程是否还活着（仅用于诊断措辞，不参与持锁判定）。"""
    if pid <= 0:
        return False
    # Linux 的 PID 空间受 /proc/sys/kernel/pid_max 限制，
    # 常见上限 4194304；超出范围的 PID 直接判死，不去问内核。
    # 不加这道闸，os.kill(1<<40, 0) 会抛 OverflowError，
    # 而 OverflowError 不是 OSError 子类，会穿透下面的 except 逃出去。
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


def _lock_fd(fd: int) -> bool:
    """对已打开的 fd 尝试非阻塞独占锁；被人占着就返回 False。"""
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_fd(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class SingleInstance:
    """基于内核文件锁的互斥锁：进程退出（含被强杀）时会自动释放。

    锁文件里的 PID 只用于把「谁在占着」讲清楚；判定一律以文件锁为准。
    注意：同进程内重复 acquire 返回 True（拿到的是同一把锁），跨进程才是真正的互斥。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.acquired = False
        self._fd: int | None = None

    def holder_pid(self) -> int | None:
        """锁文件头里记着的 PID；纯解析，不代表持有者现在真的还持锁。

        只读文件头前 32 字节：锁字节在 4KiB 处，Windows 的强制锁拦不到这里。
        """
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except OSError:
            return None
        try:
            raw = os.read(fd, _PID_READ_LEN).decode("utf-8", "replace")
        except OSError:
            return None
        finally:
            os.close(fd)
        try:
            pid = int(raw.split()[0])
        except (ValueError, IndexError):
            return None
        return pid if pid > 0 else None

    def is_held_by_other_process(self) -> bool:
        """内核锁当前是否被别的进程持有。

        试锁成功即「没人持有」，随即解锁放回去——不用读锁文件里的 PID，
        也就不会因为 PID 被系统复用而把服务永久挡在门外。
        """
        if self.acquired:
            return False
        try:
            fd = os.open(self.path, os.O_RDWR)
        except OSError:
            return False  # 文件不存在或打不开：没有人持锁
        try:
            if _lock_fd(fd):
                _unlock_fd(fd)
                return False
            return True
        finally:
            os.close(fd)

    def _lock_still_maps_to_path(self, fd: int) -> bool:
        """上锁后确认路径仍指向被锁住的那个 inode。

        上一个持锁者释放时会 unlink 锁文件；若我们的 open 与 flock 之间正好插进
        这次 unlink，我们锁住的就成了一个已经没人看得见的 inode，而别人新建的
        锁文件是另一把锁——不查这一步就会出现两个持有者。检测到就放弃，宁可失败。
        Windows 没有可靠的 st_ino，且占用中的文件删不掉，这层竞态不存在，直接放行。
        """
        if os.name == "nt":
            return True
        try:
            by_fd = os.fstat(fd)
            by_path = os.stat(self.path)
        except OSError:
            return False
        return (by_fd.st_dev, by_fd.st_ino) == (by_path.st_dev, by_path.st_ino)

    def acquire(self) -> bool:
        if self.acquired:
            return True

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            # 锁路径是目录、权限不足等一律判失败，不抛给调用方
            return False

        try:
            # PID 写文件头，锁字节在 4KiB 处；先清空再写，避免残留旧 PID 误导诊断。
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
            # msvcrt.locking 要求被锁字节真实存在，把文件撑到锁字节之后一格。
            os.ftruncate(fd, _LOCK_BYTE_OFFSET + 1)
        except OSError:
            os.close(fd)
            return False

        if not _lock_fd(fd):
            os.close(fd)
            return False
        if not self._lock_still_maps_to_path(fd):
            _unlock_fd(fd)
            os.close(fd)
            return False

        self._fd = fd
        self.acquired = True
        return True

    def release(self) -> None:
        was_acquired = self.acquired
        self.acquired = False
        fd, self._fd = self._fd, None
        if fd is not None:
            _unlock_fd(fd)
            try:
                os.close(fd)
            except OSError:
                pass
        if not was_acquired:
            return
        # 只有文件里还是我们自己的 PID 才删。接管者会在同一个位置写下它自己的 PID，
        # 所以这一步不会把别人的锁文件删掉——旧实现正是无条件 unlink 才留下竞态。
        if self.holder_pid() == os.getpid():
            self._safe_unlink()

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
