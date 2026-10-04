"""端口预检：占用了就换，或至少说清为什么起不来。

新手第一次跑 `mock_portal.py` 时若 2270 已被占用，看到的是一个
永远不返回的命令 —— 既不报错也不提示。新手只能干等，
根本不知道「端口被占」这四个字。

同一个问题也存在于主服务 `--port 2268`。两处都预检。
"""

from __future__ import annotations

import socket

# 预检时要跳过的保留/保留端口
_RESERVED = {0}


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """端口当前是否可用。

    用 bind 试探而不是 connect —— connect 只能说明「有人在听」，
    区分不出 TIME_WAIT 与真实占用。
    """
    if port in _RESERVED or not (0 < port < 65536):
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def find_free_port(
    preferred: int,
    host: str = "127.0.0.1",
    span: int = 20,
    taken: set[int] | None = None,
) -> int:
    """从 preferred 起往后找一个空闲端口。

    往后找而不是随机挑：同一台机器上端口号有规律地聚集，
    随机很可能又撞上另一个服务。

    taken 是本轮已经分配出去的端口。两个相邻端口同时被占时，
    各自独立往后找会撞到同一个空位（实测 56388/56389 都落到 56390），
    后绑的那个直接失败。必须把已分配的排除掉。
    """
    reserved = taken or set()
    for offset in range(span):
        candidate = preferred + offset
        if candidate > 65535:
            break
        if candidate in reserved:
            continue
        if is_port_free(candidate, host):
            return candidate
    raise OSError(f"从 {preferred} 开始的 {span} 个端口都被占用")


def describe_conflict(ports: dict[int, str], host: str = "127.0.0.1") -> str:
    """把冲突端口整理成一句人话，直接给用户看。"""
    lines = []
    for port, role in ports.items():
        holder = _who_holds(port, host)
        lines.append(
            f"  端口 {port}（{role}）已被占用"
            + (f"，占用者：{holder}" if holder else "")
        )
    return "\n".join(lines)


def _who_holds(port: int, host: str) -> str:
    """尽量查出是谁占着端口，查不到就说查不到，不编。"""
    try:
        if not hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # pragma: no cover - 非 Windows
            return ""
        import subprocess

        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except Exception:  # noqa: BLE001 - 诊断失败不能影响主流程
        return ""

    for line in out.splitlines():
        if f":{port}" not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        if parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
            pid = parts[4]
            return f"PID {pid}（多半是上一个没关掉的实例）"
    return ""


def resolve_ports(
    requested: dict[int, str],
    host: str = "127.0.0.1",
    auto_shift: bool = False,
    span: int = 20,
) -> dict[int, int]:
    """检查一组端口，返回实际使用的端口号。

    auto_shift 为 True 时自动往后找空闲端口；为 False 时冲突直接抛错。
    抛 OSError 而不是返回 None，调用方一眼能看出是异常路径。
    """
    resolved: dict[int, int] = {}
    conflicts: dict[int, str] = {}

    for port, role in requested.items():
        if is_port_free(port, host):
            resolved[port] = port
        else:
            conflicts[port] = role

    if not conflicts:
        return resolved

    if not auto_shift:
        raise OSError(describe_conflict(conflicts, host))

    for port, role in conflicts.items():
        found = find_free_port(port, host, span, taken=set(resolved.values()))
        resolved[port] = found
        # flush=True：stdout 被重定向到管道时是块缓冲的，
        # 提示要等攒满 8KB 才吐，用户看不到这条「已换端口」。
        print(f"[提示] 端口 {port}（{role}）被占用，已自动改用 {found}", flush=True)

    return resolved