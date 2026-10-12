"""远程关机 / 重启 / 唤醒。

开机做不到——机器断电后没有任何程序在运行，只能靠 BIOS「上电自启」、WOL
魔术包或智能插座。这里负责另一半：把关机/重启做成一条命令和一个接口，
让手机、平板也能一键操作；同时提供发 WOL 包的入口，便于在宿舍里放一台
常开设备（树莓派/路由器）替你按下电源键。
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000
WOL_PORT = 9


class PowerError(Exception):
    """关机 / 重启 / 唤醒失败。"""


def _run(args: list[str]) -> None:
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        proc = subprocess.run(args, capture_output=True, timeout=15, **kwargs)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise PowerError(f"{type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        output = ((proc.stdout or b"") + (proc.stderr or b"")).decode(
            "utf-8", "replace"
        )
        raise PowerError(output.strip() or f"命令退出码 {proc.returncode}")


def shutdown_command(delay: int = 0) -> list[str]:
    delay = max(0, int(delay))
    if sys.platform == "win32":
        return ["shutdown", "/s", "/t", str(delay)]
    if sys.platform == "darwin":
        return ["osascript", "-e", 'tell application "System Events" to shut down']
    if shutil.which("systemctl"):
        return ["systemctl", "poweroff"]
    return ["shutdown", "-h", "now"]


def reboot_command(delay: int = 0) -> list[str]:
    delay = max(0, int(delay))
    if sys.platform == "win32":
        return ["shutdown", "/r", "/t", str(delay)]
    if sys.platform == "darwin":
        return ["osascript", "-e", 'tell application "System Events" to restart']
    if shutil.which("systemctl"):
        return ["systemctl", "reboot"]
    return ["shutdown", "-r", "now"]


def shutdown(delay: int = 0, *, dry_run: bool = False) -> str:
    command = shutdown_command(delay)
    if not dry_run:
        _run(command)
    return " ".join(command)


def reboot(delay: int = 0, *, dry_run: bool = False) -> str:
    command = reboot_command(delay)
    if not dry_run:
        _run(command)
    return " ".join(command)


def magic_packet(mac: str) -> bytes:
    cleaned = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    if len(cleaned) != 12:
        raise PowerError(f"MAC 地址格式不对：{mac!r}（应形如 AA:BB:CC:DD:EE:FF）")
    return b"\xff" * 6 + bytes.fromhex(cleaned) * 16


def wake(mac: str, broadcast: str = "255.255.255.255", port: int = WOL_PORT) -> str:
    """发一个 WOL 魔术包：必须由同一二层里的设备执行才有用。"""
    packet = magic_packet(mac)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, int(port)))
    except OSError as exc:
        raise PowerError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        sock.close()
    return f"{broadcast}:{port}"
