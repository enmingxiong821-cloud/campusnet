"""密码混淆的密钥文件路径 + Windows 进程探测分支。

这两处此前零覆盖，恰好都是这台机器实际会走的路径：
密钥文件在用户目录真实读写，进程存活判定走 Windows ctypes。
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import subprocess
import sys

import pytest

from campusnet import crypto, lockfile


# ---------- decode_password 边界 ----------


def test_decode_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="未知的密码编码方式"):
        crypto.decode_password("x", "rot13")


def test_decode_plain_returns_as_is() -> None:
    assert crypto.decode_password("Abc123!", "plain") == "Abc123!"


def test_decode_base64_success() -> None:
    encoded = base64.b64encode("中文密码".encode()).decode()
    assert crypto.decode_password(encoded, "base64") == "中文密码"


@pytest.mark.parametrize(
    "bad",
    [
        "不是 base64!!",   # 非 ASCII 字符
        "###",# 非法字符
        "",                # 空串解出空串，不算失败
        "YWJ",             # 长度不是 4 的倍数
    ],
)
def test_decode_base64_failure_returns_empty(bad: str) -> None:
    """解码失败必须返回空串而不是抛异常。

    调用方拿这个值与门户响应对比，抛异常会打断整轮检测。

    注意别拿「YWJj」当失败样本 —— 那是 abc 的合法 base64。
    """
    result = crypto.decode_password(bad, "base64")
    assert result == "" or bad == ""


def test_decode_md5_base64_success() -> None:
    import base64 as b64

    digest = hashlib.md5(b"pw").hexdigest()
    encoded = b64.b64encode(digest.encode()).decode()
    assert crypto.decode_password(encoded, "md5_base64") == digest


def test_decode_md5_base64_failure_returns_empty() -> None:
    assert crypto.decode_password("!!!not base64!!!", "md5_base64") == ""


def test_decode_md5_returns_digest_unchanged() -> None:
    """MD5 不可逆，decode 只是原样返回供日志比对。"""
    digest = hashlib.md5(b"pw").hexdigest()
    assert crypto.decode_password(digest, "md5") == digest


def test_decode_sha256_returns_value() -> None:
    digest = hashlib.sha256(b"pw").hexdigest()
    assert crypto.decode_password(digest, "sha256") == digest


# ---------- 密钥文件读写 ----------


def test_env_key_overrides_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """设了环境变量就不该碰密钥文件。"""
    key_file = tmp_path / "should-not-exist.key"
    monkeypatch.setenv("CAMPUSNET_OBF_KEY", "from-env")
    monkeypatch.setenv("CAMPUSNET_KEY_FILE", str(key_file))

    key = crypto._local_key()
    assert key == hashlib.sha256(b"from-env").digest()
    assert not key_file.exists(), "环境变量模式下不该创建密钥文件"


def test_key_file_created_on_first_run(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAMPUSNET_OBF_KEY", raising=False)
    key_file = tmp_path / "sub" / "obf.key"
    monkeypatch.setenv("CAMPUSNET_KEY_FILE", str(key_file))

    key = crypto._local_key()

    assert key_file.exists(), "首次运行应写入密钥文件"
    assert len(key) == 32
    stored = key_file.read_text(encoding="utf-8").strip()
    assert base64.urlsafe_b64decode(stored.encode()) == key


def test_key_file_reused_on_second_run(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """第二次必须读到同一个密钥，否则旧密码全解不出来。"""
    monkeypatch.delenv("CAMPUSNET_OBF_KEY", raising=False)
    key_file = tmp_path / "obf.key"
    monkeypatch.setenv("CAMPUSNET_KEY_FILE", str(key_file))

    first = crypto._local_key()
    second = crypto._local_key()
    assert first == second

    # 稳定性实测：跨调用混淆同一密码仍能解回
    token = crypto.obfuscate("stable-password")
    assert crypto.deobfuscate(token) == "stable-password"


def test_corrupt_key_file_is_replaced(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """密钥文件被写坏时应重建，而不是抛异常让程序起不来。"""
    monkeypatch.delenv("CAMPUSNET_OBF_KEY", raising=False)
    key_file = tmp_path / "obf.key"
    key_file.write_text("这不是 base64!!!", encoding="utf-8")
    monkeypatch.setenv("CAMPUSNET_KEY_FILE", str(key_file))

    key = crypto._local_key()
    assert len(key) == 32
    # 文件已被覆写为合法密钥
    again = crypto._local_key()
    assert again == key


def test_key_file_unwritable_falls_back_to_random(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写不了密钥文件也要能跑（read-only 目录、权限受限）。"""
    monkeypatch.delenv("CAMPUSNET_OBF_KEY", raising=False)
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)  # 只读
    key_file = blocked / "obf.key"
    monkeypatch.setenv("CAMPUSNET_KEY_FILE", str(key_file))

    try:
        key = crypto._local_key()
        assert len(key) == 32
    finally:
        blocked.chmod(0o700)


def test_expanduser_applied_to_key_path(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """路径里的 ~ 要展开，写到字面量 "~" 目录里会静默失败。

    家目录指到 tmp_path：这条用例原本往真实家目录写文件再删掉，
    撞上用户同名的文件就是数据丢失，断言失败时还会留下垃圾。
    """
    monkeypatch.delenv("CAMPUSNET_OBF_KEY", raising=False)
    monkeypatch.setenv("CAMPUSNET_KEY_FILE", "~/test-campaignnet-obf.key")
    # expanduser 的家目录来源随平台不同（POSIX 看 HOME，Windows 看 USERPROFILE），
    # 两个都指过去，免得在另一平台上又落回真家目录。
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    crypto._local_key()

    assert (fake_home / "test-campaignnet-obf.key").exists(), "应写到展开后的用户目录"


def test_keystream_length_is_exact() -> None:
    for length in (0, 1, 31, 32, 33, 100):
        assert len(crypto._keystream(b"key", length)) == length


def test_keystream_is_deterministic() -> None:
    assert crypto._keystream(b"key", 40) == crypto._keystream(b"key", 40)


def test_keystream_prefix_stability() -> None:
    """长流的前缀应等于短流，否则分段加密会不一致。"""
    assert crypto._keystream(b"key", 40)[:16] == crypto._keystream(b"key", 16)


# ---------- Windows / POSIX 进程存活判定 ----------


@pytest.mark.parametrize("pid", [0, -1, -999])
def test_non_positive_pid_is_dead(pid: int) -> None:
    assert lockfile.pid_alive(pid) is False


def test_current_process_is_alive() -> None:
    assert lockfile.pid_alive(os.getpid()) is True


def test_impossible_pid_is_dead() -> None:
    """PID 上限之外必然不存在，且不能抛 OverflowError。

    这个断言是 Linux CI 逼出来的：os.kill 对超出 int 范围的 PID
    抛 OverflowError，而 OverflowError 不是 OSError 子类，会穿透 except 逃出去。
    持锁判定已经完全不依赖 PID，但这个诊断函数仍会被 doctor 拿去措辞，
    所以它同样不能崩。
    """
    assert lockfile.pid_alive(4_294_967_000) is False


def test_absurdly_large_pid_does_not_raise() -> None:
    """2^40 远超 Linux pid_max，必须判死而不是崩。"""
    assert lockfile.pid_alive(1 << 40) is False
    assert lockfile.pid_alive(1 << 62) is False


def test_pid_just_above_max_is_dead() -> None:
    assert lockfile.pid_alive(lockfile._MAX_PID + 1) is False


def test_current_process_is_detected_on_this_platform() -> None:
    """跨平台自检：不碰 ctypes，os.getpid 在任何平台都该判活。"""
    assert lockfile.pid_alive(os.getpid()) is True


@pytest.mark.skipif(os.name != "nt", reason="GetCurrentProcessId 仅 Windows 可用")
def test_windows_process_api_reports_current_pid() -> None:
    import ctypes

    assert ctypes.windll.kernel32.GetCurrentProcessId() == os.getpid()


def test_child_process_detected_alive_then_dead() -> None:
    """真起一个子进程：活着时判定 True，杀掉后判定 False。

    这是 Windows ctypes 分支的唯一有效验证方式——
    同一 PID 在 kill 前后判定翻转，才说明 OpenProcess 真的用对了。
    """
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert lockfile.pid_alive(proc.pid) is True
    finally:
        proc.kill()
        proc.wait(timeout=10)
    assert lockfile.pid_alive(proc.pid) is False


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 有 ctypes 分支")
def test_windows_branch_is_the_one_exercised() -> None:
    """确认当前平台确实走的是 ctypes 分支，否则上面的测试是假绿。"""
    import inspect

    src = inspect.getsource(lockfile.pid_alive)
    assert 'os.name == "nt"' in src
    assert os.name == "nt"


@pytest.mark.skipif(os.name == "nt", reason="仅 POSIX 走 os.kill 分支")
def test_posix_branch_permission_denied_counts_alive() -> None:
    """POSIX 下无权限访问别人的进程也算存活。

    pid 1 一定存在，且当前用户基本无权给它发信号：非 root 走 PermissionError 分支，
    容器里以 root 跑时 os.kill 直接成功——两条路径都该判「存活」。
    原先这里只调用不断言，等于什么都没验。
    """
    assert lockfile.pid_alive(1) is True


# ---------- 锁的边界 ----------


def test_lock_acquire_reports_holder_pid(tmp_path) -> None:  # noqa: ANN001
    lock = lockfile.SingleInstance(tmp_path / "a.lock")
    assert lock.acquire() is True
    assert lock.holder_pid() == os.getpid()
    lock.release()


def test_holder_pid_on_missing_file(tmp_path) -> None:  # noqa: ANN001
    assert lockfile.SingleInstance(tmp_path / "nope.lock").holder_pid() is None


def test_holder_pid_on_garbage_file(tmp_path) -> None:  # noqa: ANN001
    p = tmp_path / "bad.lock"
    p.write_text("不是 PID", encoding="utf-8")
    assert lockfile.SingleInstance(p).holder_pid() is None


def test_holder_pid_on_zero(tmp_path) -> None:  # noqa: ANN001
    p = tmp_path / "zero.lock"
    p.write_text("0", encoding="utf-8")
    assert lockfile.SingleInstance(p).holder_pid() is None


def test_holder_pid_on_negative(tmp_path) -> None:  # noqa: ANN001
    p = tmp_path / "neg.lock"
    p.write_text("-5", encoding="utf-8")
    assert lockfile.SingleInstance(p).holder_pid() is None


def test_holder_pid_on_multiline(tmp_path) -> None:  # noqa: ANN001
    """带多余空行/附加内容时仍应取到首行 PID。"""
    p = tmp_path / "multi.lock"
    p.write_text(f"{os.getpid()}\ncreated by campusnet\n", encoding="utf-8")
    assert lockfile.SingleInstance(p).holder_pid() == os.getpid()


def test_empty_file_is_takeoverable(tmp_path) -> None:  # noqa: ANN001
    p = tmp_path / "empty.lock"
    p.write_text("", encoding="utf-8")
    lock = lockfile.SingleInstance(p)
    assert lock.holder_pid() is None
    assert lock.acquire() is True
    lock.release()


def test_context_manager_creates_parent_dir(tmp_path) -> None:  # noqa: ANN001
    lock_path = tmp_path / "deep" / "nested" / "cn.lock"
    with lockfile.SingleInstance(lock_path) as lock:
        assert lock.acquired is True
    assert lock_path.exists() is False


def test_release_is_idempotent(tmp_path) -> None:  # noqa: ANN001
    lock = lockfile.SingleInstance(tmp_path / "x.lock")
    lock.acquire()
    lock.release()
    lock.release()  # 再调一次不该抛
    assert not lock.path.exists()


def test_acquire_after_release_reuses_lock(tmp_path) -> None:  # noqa: ANN001
    lock = lockfile.SingleInstance(tmp_path / "y.lock")
    assert lock.acquire() is True
    lock.release()
    assert lock.acquire() is True
    lock.release()


def test_is_held_by_other_process_false_for_self(tmp_path) -> None:  # noqa: ANN001
    """自己的锁不算「别人持有」，否则同进程第二次 acquire 会被自锁。"""
    lock = lockfile.SingleInstance(tmp_path / "self.lock")
    lock.acquire()
    try:
        assert lock.is_held_by_other_process() is False
    finally:
        lock.release()


def test_dir_as_lock_path_fails_gracefully(tmp_path) -> None:  # noqa: ANN001
    """锁路径是目录时 acquire 应返回 False 而不是抛异常。"""
    lock = lockfile.SingleInstance(tmp_path)  # 目录，不是文件
    assert lock.acquire() is False


def test_cross_process_blocking(tmp_path) -> None:  # noqa: ANN001
    """真起子进程验证互斥，这是 PID 文件唯一有效的验证方式。"""
    lock_path = tmp_path / "cross.lock"
    helper = (
        "import sys, time;"
        "sys.path.insert(0, sys.argv[2]);"
        "from campusnet.lockfile import SingleInstance;"
        "l = SingleInstance(sys.argv[1]);"
        "print('GOT' if l.acquire() else 'BLOCKED', flush=True);"
        "time.sleep(3)"
    )
    backend = str(__import__("pathlib").Path(__file__).resolve().parents[1])

    mine = lockfile.SingleInstance(lock_path)
    assert mine.acquire() is True

    proc = subprocess.Popen(
        [sys.executable, "-c", helper, str(lock_path), backend],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "BLOCKED"
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    mine.release()

    done = subprocess.run(
        [sys.executable, "-c", helper, str(lock_path), backend],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.stdout.strip().startswith("GOT")


# ---------- 锁的两条平台分支：两边都要走到（F2）----------
# 不这么写的话，Windows 上只覆盖 msvcrt、Linux 上只覆盖 fcntl，
# 覆盖率随跑测平台漂移，CI 双矩阵下门禁就会时红时绿。
# 做法：把 os.name 伪装成另一边，并把缺失的那个模块（fcntl / msvcrt / ctypes.windll）
# 伪造进 sys.modules。注意必须先建好 Path / SingleInstance 再改 os.name ——
# 反过来 pathlib 会按伪装后的平台去实例化，在真实平台上直接 UnsupportedOperation。


def _boom(*_args, **_kwargs):  # noqa: ANN202
    raise OSError("模拟系统调用失败")


@pytest.fixture()
def fake_fcntl(monkeypatch):  # noqa: ANN001, ANN201
    """假的 fcntl：让 POSIX 的 flock 分支在 Windows 上也能跑。"""
    import sys
    import types

    module = types.ModuleType("fcntl")
    module.LOCK_EX, module.LOCK_NB, module.LOCK_UN = 2, 4, 8
    module.calls = []
    module.flock = lambda fd, op: module.calls.append(op)
    monkeypatch.setitem(sys.modules, "fcntl", module)
    return module


@pytest.fixture()
def fake_msvcrt(monkeypatch):  # noqa: ANN001, ANN201
    """假的 msvcrt：让 Windows 的 locking 分支在 Linux 上也能跑。"""
    import sys
    import types

    module = types.ModuleType("msvcrt")
    module.LK_NBLCK, module.LK_UNLCK = 2, 0
    module.calls = []
    module.locking = lambda fd, mode, size: module.calls.append(mode)
    monkeypatch.setitem(sys.modules, "msvcrt", module)
    return module


@pytest.fixture()
def fake_kernel32(monkeypatch):  # noqa: ANN001, ANN201
    """假的 ctypes.windll.kernel32：让 Windows 进程探测在 Linux 上也能跑。"""
    import ctypes
    from types import SimpleNamespace

    class Kernel:
        def __init__(self) -> None:
            self.handle = 1
            self.exit_ok = True
            self.exit_code = 259  # STILL_ACTIVE
            self.closed: list[int] = []

        def OpenProcess(self, _access, _inherit, _pid):  # noqa: ANN001, ANN202
            return self.handle

        def GetExitCodeProcess(self, _handle, ref):  # noqa: ANN001, ANN202
            ref.value = self.exit_code
            return self.exit_ok

        def CloseHandle(self, handle: int) -> None:
            self.closed.append(handle)

    kernel = Kernel()
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(kernel32=kernel), raising=False
    )
    # ctypes.byref 返回的 CArgObject 不让写 .value，让假 kernel32 直接拿到 c_ulong
    monkeypatch.setattr(ctypes, "byref", lambda obj: obj, raising=False)
    return kernel


def _open_as(monkeypatch, path, platform):  # noqa: ANN001, ANN202
    """先按真实平台开好 fd（Path 已建好），再把 os.name 伪装过去。"""
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    monkeypatch.setattr(lockfile.os, "name", platform)
    return fd


def test_lock_fd_posix_uses_flock(monkeypatch, fake_fcntl, tmp_path) -> None:  # noqa: ANN001
    fd = _open_as(monkeypatch, tmp_path / "p.lock", "posix")
    try:
        assert lockfile._lock_fd(fd) is True
        lockfile._unlock_fd(fd)
    finally:
        os.close(fd)

    assert fake_fcntl.calls == [
        fake_fcntl.LOCK_EX | fake_fcntl.LOCK_NB,
        fake_fcntl.LOCK_UN,
    ]


def test_lock_fd_posix_reports_contention(monkeypatch, fake_fcntl, tmp_path) -> None:  # noqa: ANN001
    """别的进程占着锁时返回 False，而不是把异常抛给调用方。"""
    fd = _open_as(monkeypatch, tmp_path / "p.lock", "posix")
    fake_fcntl.flock = _boom
    try:
        assert lockfile._lock_fd(fd) is False
        lockfile._unlock_fd(fd)  # 解锁失败也只是吞掉
    finally:
        os.close(fd)


def test_lock_fd_windows_uses_msvcrt(monkeypatch, fake_msvcrt, tmp_path) -> None:  # noqa: ANN001
    fd = _open_as(monkeypatch, tmp_path / "w.lock", "nt")
    try:
        assert lockfile._lock_fd(fd) is True
        lockfile._unlock_fd(fd)
    finally:
        os.close(fd)

    assert fake_msvcrt.calls == [fake_msvcrt.LK_NBLCK, fake_msvcrt.LK_UNLCK]


def test_pid_alive_posix_kill_outcomes(monkeypatch) -> None:  # noqa: ANN001
    """POSIX 分支的几种结果：存活 / 不存在 / 无权限 / 其余异常。"""
    seen: list[tuple] = []

    def alive(pid, sig):  # noqa: ANN001, ANN202
        seen.append((pid, sig))

    def raiser(exc):  # noqa: ANN001, ANN202
        def kill(_pid, _sig):  # noqa: ANN001, ANN202
            raise exc
        return kill

    monkeypatch.setattr(lockfile.os, "name", "posix")
    monkeypatch.setattr(lockfile.os, "kill", alive)
    assert lockfile.pid_alive(1234) is True
    # 信号必须传 0：只做存在性探测，真发信号会杀掉目标进程
    assert seen == [(1234, 0)]

    monkeypatch.setattr(lockfile.os, "kill", raiser(ProcessLookupError()))
    assert lockfile.pid_alive(1234) is False

    monkeypatch.setattr(lockfile.os, "kill", raiser(PermissionError()))
    assert lockfile.pid_alive(1234) is True

    for exc in (ValueError(), OverflowError(), OSError("EINVAL")):
        monkeypatch.setattr(lockfile.os, "kill", raiser(exc))
        assert lockfile.pid_alive(1234) is False


def test_pid_alive_windows_missing_handle(monkeypatch, fake_kernel32) -> None:  # noqa: ANN001
    """OpenProcess 拿不到句柄 = 进程不存在。"""
    fake_kernel32.handle = 0
    monkeypatch.setattr(lockfile.os, "name", "nt")

    assert lockfile.pid_alive(5678) is False
    assert fake_kernel32.closed == []


def test_pid_alive_windows_exit_code_query_failed(monkeypatch, fake_kernel32) -> None:  # noqa: ANN001
    """查不到退出码时保守判为存活，宁可让 doctor 说「有人占着」。"""
    fake_kernel32.exit_ok = False
    monkeypatch.setattr(lockfile.os, "name", "nt")

    assert lockfile.pid_alive(5678) is True
    assert fake_kernel32.closed == [1]


def test_pid_alive_windows_reads_exit_code(monkeypatch, fake_kernel32) -> None:  # noqa: ANN001
    fake_kernel32.exit_code = 259  # STILL_ACTIVE
    monkeypatch.setattr(lockfile.os, "name", "nt")
    assert lockfile.pid_alive(5678) is True

    fake_kernel32.exit_code = 0
    assert lockfile.pid_alive(5678) is False
    assert fake_kernel32.closed == [1, 1]


def test_lock_still_maps_to_path_windows_shortcut(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """Windows 上占用中的文件删不掉，这层竞态不存在，直接放行。"""
    lock = lockfile.SingleInstance(tmp_path / "m.lock")
    path = lock.path
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    monkeypatch.setattr(lockfile.os, "name", "nt")
    try:
        assert lock._lock_still_maps_to_path(fd) is True
    finally:
        os.close(fd)


def test_lock_still_maps_to_path_detects_swap(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """上锁后路径被别人换掉（旧持锁者 unlink 又新建）必须判失败。

    判失败才不会出现两个持有者，这里不真去 unlink —— Windows 上打开着的文件
    删不掉，换成「路径指向别的 inode / 路径已不存在」两种等价情形。
    """
    lock = lockfile.SingleInstance(tmp_path / "a.lock")
    other = tmp_path / "b.lock"
    other.write_text("", encoding="utf-8")
    fd = os.open(lock.path, os.O_CREAT | os.O_RDWR)
    monkeypatch.setattr(lockfile.os, "name", "posix")
    try:
        assert lock._lock_still_maps_to_path(fd) is True

        lock.path = other  # 路径还在，但已经是另一个 inode
        assert lock._lock_still_maps_to_path(fd) is False

        lock.path = tmp_path / "gone.lock"  # 路径已不存在
        assert lock._lock_still_maps_to_path(fd) is False
    finally:
        os.close(fd)


def test_acquire_fails_when_lock_is_taken(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    lock = lockfile.SingleInstance(tmp_path / "t.lock")
    monkeypatch.setattr(lockfile, "_lock_fd", lambda fd: False)

    assert lock.acquire() is False
    assert lock.acquired is False
    assert lock._fd is None


def test_acquire_fails_when_inode_changed(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    lock = lockfile.SingleInstance(tmp_path / "t.lock")
    monkeypatch.setattr(lockfile, "_lock_fd", lambda fd: True)
    monkeypatch.setattr(
        lockfile.SingleInstance, "_lock_still_maps_to_path", lambda self, fd: False
    )

    assert lock.acquire() is False
    assert lock.acquired is False


def test_holder_pid_survives_read_error(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    lock = lockfile.SingleInstance(tmp_path / "r.lock")
    lock.path.write_text(str(os.getpid()) + "\n", encoding="utf-8")
    monkeypatch.setattr(lockfile.os, "read", _boom)

    assert lock.holder_pid() is None


def test_release_swallows_close_error(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    lock = lockfile.SingleInstance(tmp_path / "c.lock")
    assert lock.acquire() is True
    real_close = os.close
    failed: list[int] = []

    def close_then_fail(fd):  # noqa: ANN001, ANN202
        real_close(fd)
        # 只让第一次关失败；release 之后还会读锁文件里的 PID，那边也调 os.close，
        # 一起抛出去就测不成「release 吞掉关闭异常」了。
        if not failed:
            failed.append(fd)
            raise OSError("已经关过了")

    monkeypatch.setattr(lockfile.os, "close", close_then_fail)

    lock.release()  # 不该抛
    assert lock.acquired is False
    assert failed


def test_safe_unlink_swallows_error(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    lock = lockfile.SingleInstance(tmp_path / "u.lock")
    assert lock.acquire() is True
    monkeypatch.setattr(type(lock.path), "unlink", _boom)

    lock.release()  # 删不掉也只是留着，不该抛
    assert lock.acquired is False


def test_acquire_twice_in_same_process_is_allowed(tmp_path) -> None:  # noqa: ANN001
    """同进程重复 acquire 拿到的是同一把内核锁，直接算成功。"""
    lock = lockfile.SingleInstance(tmp_path / "again.lock")
    assert lock.acquire() is True
    try:
        assert lock.acquire() is True
    finally:
        lock.release()


def test_acquire_fails_when_touching_file_fails(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """写 PID / 撑大文件失败时返回 False，不把 OSError 抛给调用方。"""
    lock = lockfile.SingleInstance(tmp_path / "f.lock")
    monkeypatch.setattr(lockfile.os, "ftruncate", _boom)

    assert lock.acquire() is False
    assert lock.acquired is False


def test_is_held_by_other_process_on_missing_file(tmp_path) -> None:  # noqa: ANN001
    """锁文件不存在 = 没人持锁，不是「未知」。"""
    assert (
        lockfile.SingleInstance(tmp_path / "nope.lock").is_held_by_other_process()
        is False
    )


def test_is_held_by_other_process_when_file_is_free(tmp_path) -> None:  # noqa: ANN001
    """文件在但没人上锁（上次是被 kill 掉的），也要放行。"""
    path = tmp_path / "free.lock"
    path.write_text("", encoding="utf-8")

    assert lockfile.SingleInstance(path).is_held_by_other_process() is False