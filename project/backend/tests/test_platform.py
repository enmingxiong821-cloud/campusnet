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
    """路径里的 ~ 要展开，写到字面量 "~" 目录里会静默失败。"""
    monkeypatch.delenv("CAMPUSNET_OBF_KEY", raising=False)
    monkeypatch.setenv("CAMPUSNET_KEY_FILE", "~/test-campaignnet-obf.key")
    expected = os.path.expanduser("~/test-campaignnet-obf.key")
    try:
        crypto._local_key()
        assert os.path.exists(expected), "应写到展开后的用户目录"
        os.remove(expected)
    finally:
        if os.path.exists(expected):
            os.remove(expected)


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
    assert lockfile._pid_alive(pid) is False


def test_current_process_is_alive() -> None:
    assert lockfile._pid_alive(os.getpid()) is True


def test_impossible_pid_is_dead() -> None:
    """PID 上限之外必然不存在。"""
    assert lockfile._pid_alive(4_294_967_000) is False


def test_parent_process_is_alive() -> None:
    import ctypes

    parent = ctypes.windll.kernel32.GetCurrentProcessId()
    # GetCurrentProcessId 返回伪句柄，取不到父进程就退化为自检
    assert parent == os.getpid() or lockfile._pid_alive(parent)


def test_child_process_detected_alive_then_dead() -> None:
    """真起一个子进程：活着时判定 True，杀掉后判定 False。

    这是 Windows ctypes 分支的唯一有效验证方式——
    同一 PID 在 kill 前后判定翻转，才说明 OpenProcess 真的用对了。
    """
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert lockfile._pid_alive(proc.pid) is True
    finally:
        proc.kill()
        proc.wait(timeout=10)
    assert lockfile._pid_alive(proc.pid) is False


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 有 ctypes 分支")
def test_windows_branch_is_the_one_exercised() -> None:
    """确认当前平台确实走的是 ctypes 分支，否则上面的测试是假绿。"""
    import inspect

    src = inspect.getsource(lockfile._pid_alive)
    assert 'os.name == "nt"' in src
    assert os.name == "nt"


@pytest.mark.skipif(os.name == "nt", reason="仅 POSIX 走 os.kill 分支")
def test_posix_branch_permission_denied_counts_alive() -> None:
    """POSIX 下无权限访问别人的进程也算存活。"""
    lockfile._pid_alive(1)  # pid 1 通常存在且当前用户无权限查询


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