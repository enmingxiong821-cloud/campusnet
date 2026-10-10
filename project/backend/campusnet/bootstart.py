"""开机自启动：把服务注册进各系统的用户级启动项。

Windows 使用启动文件夹中的 .cmd，Linux 使用 systemd 用户单元或
XDG .desktop，macOS 使用 LaunchAgent plist。生成、查询和卸载逻辑
独立于 CLI 与 HTTP 接口，只写用户级位置，操作结果交由调用方展示。

注册是否允许取决于系统策略；启动项存在不代表已完成重启验收。
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
import sys
import xml.sax.saxutils as saxutils
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import API_HOST, API_PORT, ROOT

TASK_NAME = "campusnet"
LAUNCH_AGENT_LABEL = "com.campusnet.autostart"
SYSTEMD_UNIT = "campusnet.service"
WINDOWS_LAUNCHER = "campusnet-autostart.cmd"
LOG_NAME = "autostart.log"


@dataclass
class BootStatus:
    """开机自启动的当前状态。

    supported 区分「这套系统不支持」与「支持但没装」——
    前者是能力缺失，后者是用户没开，前端不该把它们显示成同一个东西。
    """

    supported: bool
    installed: bool
    path: Path | None
    detail: str
    success: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "supported": self.supported,
            "installed": self.installed,
            "path": str(self.path) if self.path else "",
            "detail": self.detail,
            "platform": platform_name(),
            "success": self.success,
        }


def platform_name() -> str:
    """当前平台标识。

    允许用 CAMPUSNET_FORCE_PLATFORM 覆盖，仅用于测试：三种分支的
    文件生成逻辑没法在同一台机器上真实跑一遍，只能靠注入平台名
    把每条分支都走通。
    """
    forced = os.environ.get("CAMPUSNET_FORCE_PLATFORM", "").strip().lower()
    if forced in ("windows", "linux", "darwin"):
        return forced
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux" if sys.platform.startswith("linux") else sys.platform


def python_executable() -> Path:
    """自启动要用的解释器。

    Windows 优先 pythonw.exe：它没有控制台子系统，启动时不会闪一个
    黑窗口然后悬在那里。找不到才退回 python.exe，功能一样，只是
    每次开机多一个窗口。
    """
    exe = Path(sys.executable)
    if platform_name() == "windows":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return candidate
    return exe


def run_script() -> Path:
    """utils/run.py：自带 sys.path 处理，能扛住带中文的安装路径。"""
    return ROOT / "utils" / "run.py"


def launch_args(host: str = API_HOST, port: int = API_PORT) -> list[str]:
    """启动命令的参数部分（不含解释器与脚本）。

    不带 --no-autostart：开机自启动的目的就是让监控跑起来，
    如果这里把监控关掉，等于只起了个空壳服务。
    """
    host = str(host)
    if host != "localhost":
        ipaddress.ip_address(host)
    if not 1 <= int(port) <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间")
    return ["serve", "--host", host, "--port", str(int(port))]


def launch_command(host: str = API_HOST, port: int = API_PORT) -> list[str]:
    return [str(python_executable()), str(run_script()), *launch_args(host, port)]


def log_path() -> Path:
    """自启动进程的日志落点；database/*.log 已被 .gitignore 覆盖。"""
    return ROOT / "database" / LOG_NAME


# ---------- 各平台路径 ----------


def _windows_startup_dir() -> Path:
    """当前用户的启动文件夹。

    读 APPDATA 而不是走 shell:startup：前者在测试里能直接改环境变量，
    后者要起一个 shell 才知道答案。
    """
    appdata = os.environ.get("APPDATA", "").strip()
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _linux_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return Path(xdg) if xdg else Path.home() / ".config"


def _linux_unit_path() -> Path:
    return _linux_config_dir() / "systemd" / "user" / SYSTEMD_UNIT


def _linux_desktop_path() -> Path:
    return _linux_config_dir() / "autostart" / f"{TASK_NAME}.desktop"


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def _windows_launcher_path() -> Path:
    return _windows_startup_dir() / WINDOWS_LAUNCHER


def entry_path() -> Path:
    """当前平台上自启动项的落点。"""
    name = platform_name()
    if name == "windows":
        return _windows_launcher_path()
    if name == "darwin":
        return _macos_plist_path()
    return _linux_unit_path()


# ---------- 文件内容生成 ----------


def windows_launcher_text(host: str = API_HOST, port: int = API_PORT) -> str:
    """Windows 启动项内容。

    用 start 把服务摘出去后台跑：不摘的话这个 .cmd 会一直挂着，
    开机时那个窗口就永远关不掉。

    cd 到仓库根再跑，和用户手动执行 utils\\start.bat 的行为保持一致；
    run.py 自己处理 sys.path，所以这里不需要再设 PYTHONPATH。
    """
    python = python_executable()
    return (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        f'cd /d "{ROOT}"\r\n'
        "set PYTHONIOENCODING=utf-8\r\n"
        "set PYTHONUTF8=1\r\n"
        f'start "" "{python}" "{run_script()}" {" ".join(launch_args(host, port))}'
        f' >> "{log_path()}" 2>&1\r\n'
    )


def systemd_unit_text(host: str = API_HOST, port: int = API_PORT) -> str:
    """systemd 用户单元内容。

    WantedBy=default.target 而不是 multi-user.target：用户级 systemd
    里 default.target 才对应「这个用户登录后拉起全部服务」。

    Restart=on-failure 负责进程异常退出后的重新拉起；门户登录重试
    由监控循环处理，不把单次认证失败当成进程退出。
    """
    return (
        "[Unit]\n"
        "Description=campusnet auto connect\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={str(ROOT).replace('%', '%%')}\n"
        "Environment=PYTHONIOENCODING=utf-8\n"
        "Environment=PYTHONUTF8=1\n"
        f"ExecStart={' '.join(_systemd_arg(arg) for arg in launch_command(host, port))}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _systemd_arg(value: str) -> str:
    """Quote one systemd ExecStart argument, including specifier expansion."""
    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
    escaped = escaped.replace('%', '%%').replace('$', '$$')
    escaped = escaped.replace('\n', '\\n').replace('\r', '\\r')
    return f'"{escaped}"'


def xdg_desktop_text(host: str = API_HOST, port: int = API_PORT) -> str:
    """没有 systemd 时的退路：XDG autostart。

    桌面环境（GNOME/KDE/XFCE）都会读这个目录，比强依赖 systemd 稳。
    代价是没有失败自动重启，所以只当退路用。
    """
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=campusnet\n"
        "Comment=校园网自动重连\n"
        f"Exec={' '.join(_desktop_arg(arg) for arg in launch_command(host, port))}\n"
        f"Path={ROOT}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _desktop_arg(value: str) -> str:
    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
    escaped = escaped.replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
    # Desktop entries unescape string values before parsing the Exec command.
    return '"' + escaped.replace('\\', '\\\\') + '"'


def macos_plist_text(host: str = API_HOST, port: int = API_PORT) -> str:
    """LaunchAgent plist 内容。

    RunAtLoad=true 表示用户登录时拉起。当前不配置 KeepAlive；
    网络重连由程序自己的监控循环负责，进程重启策略独立处理。
    """
    args = "".join(
        f"        <string>{saxutils.escape(item)}</string>\n"
        for item in launch_command(host, port)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{LAUNCH_AGENT_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"{args}"
        "    </array>\n"
        "    <key>WorkingDirectory</key>\n"
        f"    <string>{saxutils.escape(str(ROOT))}</string>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "    <key>StandardOutPath</key>\n"
        f"    <string>{saxutils.escape(str(log_path()))}</string>\n"
        "    <key>StandardErrorPath</key>\n"
        f"    <string>{saxutils.escape(str(log_path()))}</string>\n"
        "    <key>EnvironmentVariables</key>\n"
        "    <dict>\n"
        "        <key>PYTHONIOENCODING</key>\n"
        "        <string>utf-8</string>\n"
        "        <key>PYTHONUTF8</key>\n"
        "        <string>1</string>\n"
        "    </dict>\n"
        "</dict>\n"
        "</plist>\n"
    )


# ---------- 安装 / 卸载 / 状态 ----------


def _write_entry(path: Path, text: str) -> None:
    """原子写入启动项文件。

    先写临时文件再 replace：启动项在下次登录时会被系统读取，
    半截文件会让自启动直接失效，而且失败得毫无提示。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    try:
        temp.write_text(text, encoding="utf-8", newline="")
        temp.replace(path)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def _run_quiet(args: list[str]) -> tuple[bool, str]:
    """跑一条系统命令并合并输出；失败不抛，交给调用方决定怎么报。"""
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        proc = subprocess.run(args, capture_output=True, timeout=20, **kwargs)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    return proc.returncode == 0, (out + err).strip()


def install(host: str = API_HOST, port: int = API_PORT) -> BootStatus:
    """注册开机自启动。已存在则覆盖，不报错。"""
    name = platform_name()
    if name not in ("windows", "linux", "darwin"):
        return BootStatus(False, False, None, f"不支持的平台：{name}", False)
    path = entry_path()
    try:
        launch_args(host, port)
    except ValueError as exc:
        return BootStatus(True, False, path, f"启动参数无效：{exc}", False)
    try:
        if name == "windows":
            _write_entry(path, windows_launcher_text(host, port))
        elif name == "darwin":
            _write_entry(path, macos_plist_text(host, port))
        else:
            systemctl = shutil.which("systemctl")
            if systemctl:
                _write_entry(path, systemd_unit_text(host, port))
                ok, message = _run_quiet([systemctl, "--user", "daemon-reload"])
                if not ok:
                    return BootStatus(True, False, path, f"daemon-reload 失败：{message}", False)
                ok, message = _run_quiet([systemctl, "--user", "enable", SYSTEMD_UNIT])
                if not ok:
                    # 单元文件写成了但 enable 失败：把状态如实带出去，
                    # 不能让用户以为已经装好。
                    return BootStatus(
                        True, False, path, f"单元已写入但 enable 失败：{message}", False
                    )
            else:
                path = _linux_desktop_path()
                _write_entry(path, xdg_desktop_text(host, port))
    except OSError as exc:
        return BootStatus(True, False, path, f"写入失败：{type(exc).__name__}: {exc}", False)
    return BootStatus(True, True, path, "已注册开机自启动")


def uninstall() -> BootStatus:
    """移除开机自启动。没装过也算成功——幂等比报错更符合预期。"""
    name = platform_name()
    if name not in ("windows", "linux", "darwin"):
        return BootStatus(False, False, None, f"不支持的平台：{name}", False)
    paths = [entry_path()]
    if name == "linux":
        # 两种载体都清掉：用户可能先用 systemd 装、后来环境没了退到 desktop，
        # 只删一个会留下另一份在暗处继续拉起服务。
        paths = [_linux_unit_path(), _linux_desktop_path()]

    unit_path = _linux_unit_path()
    unit_existed = name == "linux" and unit_path.exists()
    if unit_existed:
        systemctl = shutil.which("systemctl")
        if systemctl:
            ok, message = _run_quiet([systemctl, "--user", "disable", SYSTEMD_UNIT])
            if not ok:
                return BootStatus(True, True, unit_path, f"disable 失败：{message}", False)

    removed: list[Path] = []
    for path in paths:
        try:
            if path.exists():
                path.unlink()
                removed.append(path)
        except OSError as exc:
            return BootStatus(True, True, path, f"删除失败：{type(exc).__name__}: {exc}", False)

    if unit_existed:
        systemctl = shutil.which("systemctl")
        if systemctl and not unit_path.exists():
            ok, message = _run_quiet([systemctl, "--user", "daemon-reload"])
            if not ok:
                return BootStatus(True, False, unit_path, f"启动项已删除，但 daemon-reload 失败：{message}", False)

    if removed:
        return BootStatus(True, False, removed[0], "已移除开机自启动")
    return BootStatus(True, False, None, "未安装开机自启动")


def status() -> BootStatus:
    """查询当前是否已注册。"""
    name = platform_name()
    if name not in ("windows", "linux", "darwin"):
        return BootStatus(False, False, None, f"不支持的平台：{name}")
    if name == "linux":
        desktop = _linux_desktop_path()
        if desktop.exists():
            return BootStatus(True, True, desktop, "已注册 XDG 登录自启动")
        unit = _linux_unit_path()
        if unit.exists():
            systemctl = shutil.which("systemctl")
            if not systemctl:
                return BootStatus(True, False, unit, "单元存在，但无法确认启用状态：未找到 systemctl")
            ok, message = _run_quiet([systemctl, "--user", "is-enabled", SYSTEMD_UNIT])
            enabled = ok and message.strip() == "enabled"
            return BootStatus(True, enabled, unit, "已启用 systemd 登录自启动" if enabled else f"单元未确认启用：{message}")
        return BootStatus(True, False, _linux_unit_path(), "未安装开机自启动")
    path = entry_path()
    if path.exists():
        return BootStatus(True, True, path, "已注册开机自启动")
    return BootStatus(True, False, path, "未安装开机自启动")


def activate(status_result: BootStatus) -> str:
    """把结果翻成一句给用户看的话，避免调用方各写一遍。"""
    if not status_result.success:
        return f"开机自启动操作失败：{status_result.detail}（{status_result.path}）"
    if not status_result.supported:
        return f"当前系统不支持自动配置开机自启动：{status_result.detail}"
    if status_result.installed:
        return f"开机自启动已开启（{status_result.path}）"
    if status_result.path:
        return f"开机自启动未开启：{status_result.detail}（{status_result.path}）"
    return f"开机自启动未开启：{status_result.detail}"
