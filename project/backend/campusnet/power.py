"""远程关机 / 重启 / 唤醒。

开机做不到——机器断电后没有任何程序在运行，只能靠 BIOS「上电自启」、WOL
魔术包或智能插座。这里负责另一半：把关机/重启做成一条命令和一个接口，
让手机、平板也能一键操作；同时提供发 WOL 包的入口，便于在宿舍里放一台
常开设备（树莓派/路由器）替你按下电源键。
"""

from __future__ import annotations

import ipaddress
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
        if _macos_gui_session():
            return ["osascript", "-e", 'tell application "System Events" to shut down']
        return ["shutdown", "-h", "now"]
    if shutil.which("systemctl"):
        return ["systemctl", "poweroff"]
    return ["shutdown", "-h", "now"]


def reboot_command(delay: int = 0) -> list[str]:
    delay = max(0, int(delay))
    if sys.platform == "win32":
        return ["shutdown", "/r", "/t", str(delay)]
    if sys.platform == "darwin":
        if _macos_gui_session():
            return ["osascript", "-e", 'tell application "System Events" to restart']
        return ["shutdown", "-r", "now"]
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


def _macos_gui_session() -> bool:
    """当前进程有没有图形登录会话。

    macOS 的 `osascript` 要经 Apple Events 找到 System Events，而它只存在于
    Aqua 会话里。LaunchDaemon（无人登录）与 SSH 会话都拿不到，这时 osascript
    会失败，用户看到的是「点了关机，什么都没发生」——而这两种恰恰是
    「无需登录」的目标场景本身，所以不能只认 osascript。

    `launchctl managername` 是最省事的判据：图形会话是 Aqua，
    LaunchDaemon 是 System，SSH 是 Background / StandardIO。
    这里只认「输出里有 Aqua」，不去逐字比对某个固定串——判据的关键是
    「在不在图形域里」，多几个空白或前缀都不该让它失效。
    """
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if not shutil.which("launchctl"):
        return False
    ok, text = _run_capture(["launchctl", "managername"])
    return ok and "aqua" in text.strip().lower()


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


_IPV4_RE = re.compile(r"\d+\.\d+\.\d+\.\d+")

# POSIX 两种 ifconfig 写法：BSD 是 `netmask 0xffffff00`，net-tools 是
# `Mask:255.255.255.0` / `netmask 255.255.255.0`。
_INET_MASK_RE = re.compile(
    r"\binet (?:addr:)?(\d+\.\d+\.\d+\.\d+).*?"
    r"(?:netmask|Mask:?)\s*(0x[0-9a-fA-F]{8}|\d+\.\d+\.\d+\.\d+)"
)


def _prefix_from_mask(mask: str) -> int | None:
    """点分十进制掩码转前缀长度；不是连续掩码（如组播的 240.0.0.0）返回 None。"""
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
    except ValueError:
        return None


def _as_dotted(value: str) -> str:
    """ifconfig 的掩码是十六进制，换算成点分十进制再交给 ipaddress。"""
    if not value.startswith("0x"):
        return value
    return str(ipaddress.IPv4Address(int(value, 16)))


def local_ipv4_networks(platform: str | None = None) -> list[tuple[str, int]]:
    """本机所在的 IPv4 子网，返回 (网络地址, 前缀长度)；取不到就返回空列表。

    定向广播要靠前缀长度才算得出来，而 `socket` 没有暴露掩码的接口，只能问系统。
    三条命令各管一个平台：

    - Windows 走 `route print -4`。表头是本地化的（「网络目标」「在链路上」），
      但**数据行全是数字**，所以按「第 1、2、4 个字段都是点分十进制」来认，
      不碰任何标签文字——这也顺带排掉了「永久路由」那种只有四段的写法。
    - Linux 的 `ip -o -4 addr show` 自带 `inet 10.0.0.5/16`，最省事。
    - macOS/BSD 只有 `ifconfig -a`，掩码是十六进制的。

    platform 参数供测试注入，与 local_macs() 同一约定。
    """
    name = platform or ("nt" if os.name == "nt" else "posix")
    found: list[tuple[str, int]] = []
    if name == "nt":
        ok, text = _run_capture(["route", "print", "-4"])
        if not ok:
            return []
        for line in text.splitlines():
            fields = line.split()
            if len(fields) < 4:
                continue
            dest, mask, iface = fields[0], fields[1], fields[3]
            if not (
                _IPV4_RE.fullmatch(dest)
                and _IPV4_RE.fullmatch(mask)
                and _IPV4_RE.fullmatch(iface)
            ):
                continue
            prefix = _prefix_from_mask(mask)
            # 0.0.0.0/0 是默认路由；/31、/32 是每条网卡地址自己的主机路由，
            # 它们也走这张表，但都不是「一个子网」。
            if prefix is None or not 1 <= prefix <= 30:
                continue
            if dest.startswith(("127.", "224.")):
                continue
            found.append(_network_entry(dest, prefix))
        return list(dict.fromkeys(found))
    ok, text = _run_capture(["ip", "-o", "-4", "addr", "show"])
    if ok:
        for match in re.finditer(r"\binet (\d+\.\d+\.\d+\.\d+)/(\d+)", text or ""):
            prefix = int(match.group(2))
            if 1 <= prefix <= 30 and not match.group(1).startswith("127."):
                found.append(_network_entry(match.group(1), prefix))
    if not found:
        ok, text = _run_capture(["ifconfig", "-a"])
        if not ok:
            return []
        for match in _INET_MASK_RE.finditer(text or ""):
            address, mask = match.group(1), _as_dotted(match.group(2))
            prefix = _prefix_from_mask(mask)
            if prefix is None or not 1 <= prefix <= 30:
                continue
            if address.startswith("127."):
                continue
            found.append(_network_entry(address, prefix))
    return list(dict.fromkeys(found))


def _network_entry(address: str, prefix: int) -> tuple[str, int]:
    net = ipaddress.IPv4Network(f"{address}/{prefix}", strict=False)
    return str(net.network_address), prefix


def directed_broadcast(host: str, platform: str | None = None) -> str:
    """[host] 所在子网的定向广播地址；判断不出就返回空串。

    定向广播比全局广播 255.255.255.255 稳——部分 Android 版本会拦下后者。
    host 传控制台的 Host 头，也就是手机实际访问的那个地址：由它来定「哪个子网」
    不用猜。反过来若拿默认路由出口的地址去认，本机挂着的 VPN、Hyper-V、WSL
    随便哪条都可能把它带偏。
    """
    try:
        target = ipaddress.IPv4Address(host.strip())
    except ValueError:
        return ""
    for network, prefix in local_ipv4_networks(platform):
        net = ipaddress.IPv4Network(f"{network}/{prefix}")
        if target in net:
            return str(net.broadcast_address)
    return ""


def wake_info(port: int = WOL_PORT, host: str = "") -> dict[str, object]:
    """远程开机需要的信息：网卡 MAC、唤醒命令，以及三条硬件路线的说明。

    host 是控制台的 Host 头，用来定定向广播地址；判断不出就退回全局广播——
    调用方（手机 APP）会把「等于全局广播」当成「没给」，自己去猜 /24。
    """
    macs = local_macs()
    command = (
        f"python utils/run.py power wake --mac {macs[0]}"
        if macs
        else "python utils/run.py power wake --mac <网卡MAC>"
    )
    return {
        "macs": macs,
        "broadcast": directed_broadcast(host) or "255.255.255.255",
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
    try:
        port_num = int(port)
    except (TypeError, ValueError) as exc:
        raise PowerError(f"端口非法：{port!r}") from exc
    # 越界端口 sendto 抛的是 OverflowError（不是 OSError），不先挡掉的话会一路
    # 冒到调用方，计划里那条也只能留下一条看不懂的 warning。
    if not 1 <= port_num <= 65535:
        raise PowerError(f"端口 {port_num} 超出 1-65535")
    packet = magic_packet(mac)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, port_num))
    except OSError as exc:
        raise PowerError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        sock.close()
    return f"{broadcast}:{port_num}"
