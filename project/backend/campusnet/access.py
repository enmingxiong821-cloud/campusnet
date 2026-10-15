"""多端访问信息：把「手机/平板怎么连上这台电脑」需要的东西一次列清楚。

用户真正会卡住的地方不是认证，而是「我人在外面，怎么知道该访问哪个地址、
要不要令牌」。所以这里把本机地址、局域网地址、Tailscale 地址与令牌一次打出来。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from typing import Any

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
    lines.append(
        "手机快捷方式    控制台打开后点「添加到主屏幕」，以后像 App 一样一键打开"
        "（远程接入看 run.py access --guide）"
    )
    if host in ("127.0.0.1", "localhost", "::1"):
        lines.append(
            "提示            服务当前只监听本机，手机访问不了："
            "改用 --host 0.0.0.0 启动，或让两台设备都进同一个 Tailscale 网络"
        )
    return lines


def tailscale_status() -> dict[str, Any]:
    """Tailscale 的安装 / 运行 / 登录状态与对端数量。

    用 `tailscale status --json` 拿结构化信息（Self 的 DNSName / TailscaleIPs、
    Peer 数量）。任何一步失败都降级成「没检测到」，不让向导崩掉。
    """
    empty = {
        "installed": False,
        "running": False,
        "logged_in": False,
        "tailnet": "",
        "hostname": "",
        "ip": "",
        "peers": 0,
    }
    exe = shutil.which("tailscale")
    if not exe:
        return empty
    try:
        proc = subprocess.run([exe, "status", "--json"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {"installed": True, "running": False, "logged_in": False,
                "tailnet": "", "hostname": "", "ip": "", "peers": 0}
    if proc.returncode != 0:
        return {"installed": True, "running": False, "logged_in": False,
                "tailnet": "", "hostname": "", "ip": "", "peers": 0}
    try:
        data = json.loads((proc.stdout or b"").decode("utf-8", "replace"))
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    self_info = data.get("Self") or {}
    if not isinstance(self_info, dict):
        self_info = {}
    peers = data.get("Peer") or {}
    ips = self_info.get("TailscaleIPs") or []
    ipv4 = next((str(ip) for ip in ips if ":" not in str(ip)), "")
    return {
        "installed": True,
        "running": True,
        "logged_in": True,
        "tailnet": _tailnet_name(data),
        "hostname": str(self_info.get("HostName", "")),
        "ip": ipv4,
        "peers": len(peers) if isinstance(peers, (dict, list)) else 0,
    }


def _tailnet_name(data: dict[str, Any]) -> str:
    current = data.get("CurrentTailnet") or {}
    name = str(current.get("Name", "")) if isinstance(current, dict) else ""
    if name.endswith(".ts.net"):
        name = name[: -len(".ts.net")]
    if name:
        return name
    dns = str((data.get("Self") or {}).get("DNSName", ""))
    parts = dns.split(".")
    return parts[1] if len(parts) >= 3 else ""


def guide(host: str = API_HOST, port: int = API_PORT) -> list[str]:
    """远程接入三步向导：装 Tailscale → 装远程桌面 → 手机怎么点。"""
    status = tailscale_status()
    lines = ["远程接入三步走（只检测与指引，不改网络配置）：", ""]

    if not status["installed"]:
        lines += [
            "① 装 Tailscale —— 缺这一步（本机没检测到 tailscale）",
            "   目的：人不在宿舍时，任何网络下都能连回这台电脑",
            "   Windows: winget install tailscale.tailscale",
            "   macOS:   brew install --cask tailscale",
            "   Linux:   curl -fsSL https://tailscale.com/install.sh | sh",
            "   下载页：https://tailscale.com/download ；装完跑 `tailscale up` 登录",
        ]
    elif not status["logged_in"]:
        lines.append("① Tailscale 已安装但未登录/未运行 —— 跑 `tailscale up` 登录")
    else:
        lines.append(
            f"① Tailscale 已登录：tailnet={status['tailnet'] or '?'}"
            f"，对端 {status['peers']} 台"
        )

    lines += [
        "",
        "② 装远程桌面（RustDesk 优先，国产网络友好）",
        "   下载 https://rustdesk.com/zh/ ，电脑和手机都装；记下电脑那台的「ID + 密码」",
        "",
        "③ 手机端访问控制台",
    ]
    if status["ip"]:
        lines.append(f"   Tailscale 地址：http://{status['ip']}:{port}")
    ip = lan_ip()
    if ip:
        lines.append(f"   局域网地址（同一 Wi-Fi 下）：http://{ip}:{port}")
    if not status["ip"] and not ip:
        lines.append("   先装好 ① 的 Tailscale，这里才会有手机能用的地址")
    if host in ("127.0.0.1", "localhost", "::1"):
        lines.append("   提示：服务当前只监听本机，手机访问需以 --host 0.0.0.0 启动")
    if os.name == "nt":
        # 按上面那步改了还连不上，卡点基本都在这里：Windows 防火墙的入站规则。
        # 那个允许框只在首次监听非回环地址时弹一次，点过「取消」就不再弹，
        # 表面症状是「手机一直转圈」。
        lines.append("   提示：Windows 首次以 --host 0.0.0.0 启动会弹防火墙允许框，")
        lines.append("         勾上「专用网络」再点允许；误点过取消就不会再弹，")
        lines.append("         到「Windows 安全中心 → 防火墙和网络保护 → 允许应用通过防火墙」补上")
    lines.append("   打开后点「添加到主屏幕」，之后能一键看状态 / 关机 / 唤醒")
    return lines
