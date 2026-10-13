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


def _run_capture(args: list[str]) -> tuple[bool, str]:
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        proc = subprocess.run(args, capture_output=True, timeout=15, **kwargs)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False, ""
    text = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
    return proc.returncode == 0, text


def local_macs(platform: str | None = None) -> list[str]:
    """本机网卡 MAC（大写、冒号分隔）；取不到就返回空列表。

    WOL 必须填「目标网卡」的 MAC，用户最容易卡在这一步，所以直接给出来。
    platform 参数供测试注入，避免为了测试去改全局的 os.name。
    """
    name = platform or ("nt" if os.name == "nt" else "posix")
    if name == "nt":
        exe = shutil.which("getmac")
        if not exe:
            return []
        ok, text = _run_capture([exe, "/fo", "csv", "/nh"])
        if not ok:
            return []
        entries: list[tuple[str, bool]] = []
        for line in text.splitlines():
            match = re.match(
                r'\s*"([0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5})"\s*,\s*"([^"]*)"', line
            )
            if not match:
                continue
            mac = match.group(1).upper().replace("-", ":")
            transport = match.group(2).lower()
            connected = "disconnected" not in transport and "n/a" not in transport
            if mac not in [item[0] for item in entries]:
                entries.append((mac, connected))
        # 已连接的网卡排在前面：唤醒命令默认取第一个，而 WOL 必须打在
        # 真正在用的那块网卡上（断开的 Wi-Fi 唤醒不了）。
        entries.sort(key=lambda item: not item[1])
        return [mac for mac, _ in entries]
    ok, text = _run_capture(["ip", "-o", "link"])
    if not ok:
        ok, text = _run_capture(["ifconfig", "-a"])
    macs = []
    for match in re.finditer(r"\b([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\b", text or ""):
        mac = match.group(1).upper()
        # `ip -o link` / `ifconfig -a` 的输出里带着广播地址与全零占位，
        # 它们长得和 MAC 一样但不是网卡地址，留给用户只会让人填错 WOL。
        if mac in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
            continue
        if mac not in macs:
            macs.append(mac)
    return macs


def wake_info(port: int = WOL_PORT) -> dict[str, object]:
    """远程开机需要的信息：网卡 MAC、唤醒命令，以及三条硬件路线的说明。"""
    macs = local_macs()
    command = (
        f"python utils/run.py power wake --mac {macs[0]}"
        if macs
        else "python utils/run.py power wake --mac <网卡MAC>"
    )
    return {
        "macs": macs,
        "broadcast": "255.255.255.255",
        "port": port,
        "command": command,
        "notes": [
            "开机这件事软件做不到：机器断电后没有任何程序在运行。",
            "最稳的是 BIOS 里把「断电恢复」设为 Power On，再用智能插座断电几秒重新上电。",
            "手机装个 WOL App，填上面的 MAC（已连接的网卡排在前面），同一局域网内可以直接唤醒。",
            "宿舍里长期可靠的做法：留一台常开小设备（树莓派/ESP32/旧路由）跑上面那条命令。",
        ],
    }


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
