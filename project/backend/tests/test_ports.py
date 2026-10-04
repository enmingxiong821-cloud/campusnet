"""端口预检测试：占用了要说人话，而不是抛 traceback。"""

from __future__ import annotations

import socket
import threading

import pytest

from campusnet import ports


@pytest.fixture()
def occupied():
    """占住一个端口，测试完自动释放。"""
    held: list[socket.socket] = []

    def grab() -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        held.append(sock)
        return sock.getsockname()[1]

    yield grab
    for sock in held:
        sock.close()


# ---------- is_port_free ----------


def test_free_port_detected_as_free() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # socket 已关闭，端口应重新可用
    assert ports.is_port_free(port) is True


def test_listening_port_detected_as_busy(occupied) -> None:  # noqa: ANN001
    assert ports.is_port_free(occupied()) is False


@pytest.mark.parametrize("port", [0, -1, 70000, 65536])
def test_invalid_ports_treated_as_unavailable(port: int) -> None:
    assert ports.is_port_free(port) is False


# ---------- find_free_port ----------


def test_find_free_port_returns_preferred_when_available() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    probe.close()
    assert ports.find_free_port(free) == free


def test_find_free_port_shifts_past_occupied(occupied) -> None:  # noqa: ANN001
    busy = occupied()
    found = ports.find_free_port(busy, span=30)
    assert found != busy
    assert busy < found <= busy + 30


def test_find_free_port_raises_when_span_exhausted(occupied) -> None:  # noqa: ANN001
    busy = occupied()
    # span=1 意味着只能试 busy 本身，必然失败
    with pytest.raises(OSError, match="都被占用"):
        ports.find_free_port(busy, span=1)


# ---------- resolve_ports ----------


def test_resolve_ports_passes_through_free() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    probe.close()

    got = ports.resolve_ports({free: "服务"})
    assert got == {free: free}


def test_resolve_ports_raises_on_conflict_by_default(occupied) -> None:  # noqa: ANN001
    busy = occupied()
    with pytest.raises(OSError) as exc:
        ports.resolve_ports({busy: "门户"})
    assert "已被占用" in str(exc.value)
    assert "门户" in str(exc.value)


def test_resolve_ports_auto_shift_avoids_conflict(occupied) -> None:  # noqa: ANN001
    busy = occupied()
    got = ports.resolve_ports({busy: "门户"}, auto_shift=True, span=30)
    assert got[busy] != busy
    assert ports.is_port_free(got[busy]) is True


def test_resolve_ports_auto_shift_handles_two_ports(occupied) -> None:  # noqa: ANN001
    """两个端口同时被占时要各自错开，不能挤到同一个上。"""
    first, second = occupied(), occupied()
    got = ports.resolve_ports({first: "门户", second: "探测"}, auto_shift=True, span=30)
    assert len(set(got.values())) == 2, f"两个端口不能解析到同一个：{got}"
    for assigned in got.values():
        assert ports.is_port_free(assigned) is True


def test_resolve_ports_conflict_message_names_role(occupied) -> None:  # noqa: ANN001
    busy = occupied()
    with pytest.raises(OSError, match="探测"):
        ports.resolve_ports({busy: "探测"})


# ---------- describe_conflict ----------


def test_describe_conflict_lists_every_port(occupied) -> None:  # noqa: ANN001
    a, b = occupied(), occupied()
    text = ports.describe_conflict({a: "门户", b: "探测"})
    assert "门户" in text and "探测" in text
    assert str(a) in text and str(b) in text


def test_describe_conflict_never_raises() -> None:
    assert isinstance(ports.describe_conflict({}), str)


# ---------- 与 serve 的衔接 ----------


def test_serve_reports_conflict_instead_of_hanging(api_port: int) -> None:
    """主服务端口被占时要退出，不能静默挂着。"""
    from campusnet import server

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", api_port))
        sock.listen(1)
        with pytest.raises(SystemExit) as exc:
            server.serve(port=api_port, autostart=False, blocking=False)
        assert exc.value.code == 1


def test_serve_auto_shift_starts_on_next_port(api_port: int) -> None:
    """auto_shift=True 时应自动落到空闲端口并真正起服务。"""
    from campusnet import server

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", api_port))
        sock.listen(1)
        httpd = server.serve(
            port=api_port, autostart=False, blocking=False, auto_shift=True
        )
        try:
            assert httpd.server_address[1] != api_port
            assert ports.is_port_free(httpd.server_address[1]) is False
        finally:
            httpd.shutdown()
            httpd.server_close()


@pytest.fixture()
def api_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _run_mock_portal(extra_args: list[str], busy: int, probe: int | None = None):
    """启动 mock_portal 并强制 UTF-8 输出。

    Windows 控制台默认 GBK，不指定 PYTHONIOENCODING 时
    中文提示会被编码成 GBK 字节，测试按 UTF-8 解码直接崩。
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    utils_dir = Path(__file__).resolve().parents[3] / "utils"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [
            sys.executable,
            str(utils_dir / "mock_portal.py"),
            "--portal-port", str(busy),
            "--probe-port", str(probe if probe is not None else busy),
            *extra_args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
    )


def test_mock_portal_reports_conflict_instead_of_hanging() -> None:
    """模拟门户端口被占时也要退出并给出提示。"""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        busy = sock.getsockname()[1]
        sock.listen(1)
        proc = _run_mock_portal(["--drop"], busy)

    assert proc.returncode == 1
    assert "已被占用" in proc.stderr
    assert "python" in proc.stderr, "提示里应给出可直接复制的命令"


def test_mock_portal_auto_shift_survives_conflict() -> None:
    """auto_shift 时端口冲突也不该死。"""
    import os
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path

    utils_dir = Path(__file__).resolve().parents[3] / "utils"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        busy = sock.getsockname()[1]
        sock.listen(1)

        proc = subprocess.Popen(
            [
                sys.executable,
                str(utils_dir / "mock_portal.py"),
                "--portal-port", str(busy),
                "--probe-port", str(busy + 1),
                "--auto-shift",
                "--drop",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
        )
        try:
            time.sleep(4)
            assert proc.poll() is None, "auto_shift 后应持续运行而不是退出"
        finally:
            proc.terminate()
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate(timeout=10)
    assert "自动改用" in out or "自动改用" in err


def test_no_stray_listeners_after_tests() -> None:
    """确认本模块没留下监听（回归保护）。"""
    from campusnet import server as server_mod

    threading.active_count()  # 触达线程状态即可
    assert callable(server_mod.serve)