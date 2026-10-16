"""多端访问信息：把「手机/平板怎么连上这台电脑」需要的东西一次列清楚。

用户真正会卡住的地方不是认证，而是「我人在外面，怎么知道该访问哪个地址、
要不要令牌」。所以这里把本机地址、局域网地址、Tailscale 地址与令牌一次打出来。
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
from typing import Any

from .auth import ApiAuth
from .charset import decode as decode_text
from .paths import API_HOST, API_PORT

PROBE_TARGET = "8.8.8.8"
CREATE_NO_WINDOW = 0x08000000

# 虚拟网卡的名字是系统或厂商在安装时写死的英文，不跟着界面语言走——而真网卡的
# 显示名恰好相反（中文系统上是「以太网」「WLAN」）。所以这里用黑名单：认不出
# 名字的一律当成真网卡，宁可留着也不误杀。
_VIRTUAL_NAME_HINTS = (
    "hyper-v",
    "vethernet",
    "wsl",
    "virtual",
    "vmware",
    "virtualbox",
    "tailscale",
    "zerotier",
    "wireguard",
    "openvpn",
    "tunnel",
    "tun",
    "tap",
    "wintun",
    "clash",
    "mihomo",
    "docker",
    "veth",
    "br-",
    "virbr",
    "loopback",
    "bluetooth",
    "npcap",
)

# RFC 2544 拿这段做网络设备基准测试，Clash / Mihomo 之类用它当 TUN 的 fake-ip
# 网段，真实局域网不会落在里面。
_NON_LAN_NETWORKS = (ipaddress.IPv4Network("198.18.0.0/15"),)

_ADDRESS_LINE_RE = re.compile(r":\s*(\d+\.\d+\.\d+\.\d+)\s*(?:\([^)]*\))?$")
_ADAPTER_NAME_RE = re.compile(r'"([^"]+)"')
_IFACE_METRIC_RE = re.compile(r"^\s*InterfaceMetric\s*:\s*(\d+)")
_IP_ADDR_RE = re.compile(
    r"^\s*\d+:\s+(\S+)\s+inet (\d+\.\d+\.\d+\.\d+)", re.M
)
_IFCONFIG_NAME_RE = re.compile(r"^(\S+?):\s")
_IFCONFIG_INET_RE = re.compile(r"^\s+inet (\d+\.\d+\.\d+\.\d+)")


def _run_text(args: list[str], timeout: float = 10) -> tuple[bool, str]:
    """跑一条系统命令并解码输出。

    不能按 UTF-8 硬解：实测 netsh 的输出是 UTF-8，而 route / ipconfig 是 GBK
    （中文系统的 ANSI 代码页），所以交给 charset.decode 按
    「UTF-8 严格 → GB18030 → latin-1」自己挑。
    """
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout, **kwargs)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False, ""
    return proc.returncode == 0, decode_text(proc.stdout or b"")


def _windows_addresses() -> list[tuple[str, str]]:
    """`netsh interface ipv4 show addresses` → [(网卡名, 地址)]，按接口跃点升序。

    一条命令就同时给出网卡名、地址和跃点——route print -4 只有「接口 IP」列，
    网卡名在另一段的「接口列表」里，路由表里没有 ifindex，两边拼不起来。
    表头是本地化的，所以不认任何标签：名字取块头引号里的内容（实测是 ASCII
    引号），地址取块内第一条「冒号后直接是点分十进制、到行尾为止」的行，也就是
    IP 地址那一行（默认网关排在它后面；子网前缀带着 /16，匹配不上）。跃点字段
    InterfaceMetric 本身就是英文。
    """
    ok, text = _run_text(["netsh", "interface", "ipv4", "show", "addresses"])
    if not ok:
        return []
    entries: list[tuple[str, str, int]] = []
    for block in re.split(r"\n(?=\S)", text):
        lines = block.splitlines()
        if not lines:
            continue
        name_match = _ADAPTER_NAME_RE.search(lines[0])
        if not name_match:
            continue
        address = ""
        metric = 10**9
        for line in lines[1:]:
            if not address:
                hit = _ADDRESS_LINE_RE.search(line)
                if hit:
                    address = hit.group(1)
            metric_hit = _IFACE_METRIC_RE.match(line)
            if metric_hit:
                metric = int(metric_hit.group(1))
        if address:
            entries.append((name_match.group(1), address, metric))
    entries.sort(key=lambda item: item[2])
    return [(name, address) for name, address, _ in entries]


def _posix_addresses() -> list[tuple[str, str]]:
    """Linux 走 `ip -o -4 addr show`；只有 macOS/BSD 回退到 `ifconfig -a`。"""
    ok, text = _run_text(["ip", "-o", "-4", "addr", "show"])
    if ok:
        found = [
            (match.group(1), match.group(2)) for match in _IP_ADDR_RE.finditer(text)
        ]
        if found:
            return found
    ok, text = _run_text(["ifconfig", "-a"])
    if not ok:
        return []
    found = []
    name = ""
    for line in text.splitlines():
        header = _IFCONFIG_NAME_RE.match(line)
        if header:
            name = header.group(1)
            continue
        inet = _IFCONFIG_INET_RE.match(line)
        if inet and name:
            found.append((name, inet.group(1)))
    return found


def local_ipv4_addresses(platform: str | None = None) -> list[tuple[str, str]]:
    """本机每块网卡的 (名字, IPv4)，取不到就返回空列表。

    platform 参数供测试注入，与 power.local_ipv4_networks() 同一约定。
    """
    name = platform or ("nt" if os.name == "nt" else "posix")
    return _windows_addresses() if name == "nt" else _posix_addresses()


def _exit_ip() -> str:
    """默认路由出口上的本机 IPv4（UDP connect 不发包，只让内核查路由表）。

    只能当「系统偏好哪块网卡」的提示，不能直接当局域网地址：本机挂着 Clash
    这类 TUN 时它的默认路由跃点是 0，出口地址会是 198.18.0.1 这种虚拟地址。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.5)
        sock.connect((PROBE_TARGET, 1))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def _is_lan_address(ip: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    if (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
    ):
        return False
    return not any(addr in net for net in _NON_LAN_NETWORKS)


def _is_virtual_name(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _VIRTUAL_NAME_HINTS)


def lan_ip() -> str:
    """局域网地址：直接挑一块真网卡，而不是问「上网走哪条路」。

    同网段多块网卡（有线 + 无线同时在线）时优先系统自己偏好的那块，即默认路由
    的出口地址；那块恰好是虚拟网卡或没有候选匹配时，退回枚举顺序（Windows 上
    已按接口跃点升序）。
    """
    addresses = local_ipv4_addresses()
    if not addresses:
        # 枚举命令不可用（精简系统、非预期平台）：退回老办法，总比没有强。
        return _exit_ip()
    exit_ip = _exit_ip()
    fallback = ""
    for name, ip in addresses:
        if not _is_lan_address(ip) or _is_virtual_name(name):
            continue
        if ip == exit_ip:
            return ip
        if not fallback:
            fallback = ip
    return fallback


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
