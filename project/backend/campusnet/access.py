"""多端访问信息：把「手机/平板怎么连上这台电脑」需要的东西一次列清楚。

用户真正会卡住的地方不是认证，而是「我人在外面，怎么知道该访问哪个地址、
要不要令牌」。所以这里把本机地址、局域网地址、Tailscale 地址与令牌一次打出来。
"""

from __future__ import annotations

import shutil
import socket
import subprocess

from .auth import ApiAuth
from .paths import API_HOST, API_PORT

PROBE_TARGET = "8.8.8.8"


def lan_ip() -> str:
    """默认路由出口上的本机 IPv4（UDP connect 不发包，只让内核查路由表）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.5)
        sock.connect((PROBE_TARGET, 1))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def tailscale_ip() -> str:
    """装了 Tailscale 且已登录时返回它的 IPv4，否则空串。"""
    exe = shutil.which("tailscale")
    if not exe:
        return ""
    try:
        proc = subprocess.run([exe, "ip", "-4"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    text = (proc.stdout or b"").decode("utf-8", "replace").strip()
    return text.splitlines()[0].strip() if text else ""


def token_text() -> str:
    try:
        return ApiAuth().token
    except Exception:  # noqa: BLE001 - 读令牌失败不该让整条命令崩掉
        return ""


def report(host: str = API_HOST, port: int = API_PORT) -> list[str]:
    lines = [f"本机控制台      http://127.0.0.1:{port}"]
    ip = lan_ip()
    if ip:
        lines.append(f"局域网控制台    http://{ip}:{port}")
    tailscale = tailscale_ip()
    if tailscale:
        lines.append(f"Tailscale 地址  http://{tailscale}:{port}   （任何网络下都能用）")
    lines.append(f"控制台令牌      {token_text() or '（读不到，可跑 run.py token）'}")
    lines.append("手机端建议      装 Tailscale；远程桌面用 RDP 客户端或 RustDesk")
    if host in ("127.0.0.1", "localhost", "::1"):
        lines.append(
            "提示            服务当前只监听本机，手机访问不了："
            "改用 --host 0.0.0.0 启动，或让两台设备都进同一个 Tailscale 网络"
        )
    return lines
