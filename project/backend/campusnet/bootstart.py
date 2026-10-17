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
from typing import Any, Sequence

from .charset import decode_bytes
from .paths import API_HOST, API_PORT, ROOT, frozen_prefix

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
    #: 这个载体在本机是否真的会拉起服务。装了但不会生效（没有桌面会话的 XDG、
    #: 没开 systemd 的 WSL）必须和「已开启」区分开，否则用户重启后才发现没联网。
    effective: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "supported": self.supported,
            "installed": self.installed,
            "path": str(self.path) if self.path else "",
            "detail": self.detail,
            "platform": platform_name(),
            "success": self.success,
            "effective": self.effective,
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


def is_wsl() -> bool:
    """是否跑在 WSL 里。

    WSL 的 os.name 是 posix、sys.platform 是 linux，但默认没有 systemd、
    也没有桌面会话：XDG autostart 永远不会被执行，用户却会看到「已开启」。
    WSL2 的 systemd 属于要手动开启的特性，所以这里必须显式识别。
    """
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    if sys.platform != "linux":
        return False
    try:
        version = Path("/proc/version").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "microsoft" in version.lower()


def has_desktop_session() -> bool:
    """桌面会话线索：XDG autostart 只在桌面会话里被读取。"""
    return any(
        os.environ.get(name)
        for name in (
            "XDG_CURRENT_DESKTOP",
            "DESKTOP_SESSION",
            "DISPLAY",
            "WAYLAND_DISPLAY",
        )
    )


def unattended_hint() -> str:
    """「无需登录」在本机该怎么做到。

    用户级载体一律是「登录后」语义——这是刻意的取舍（不写系统级位置、不动账号级
    设置），不是没装好。替代路径每个平台都不一样，散在 install / status / CLI / 菜单
    里各写一遍必然走样，所以收在这一处。
    """
    name = platform_name()
    if name == "windows":
        return "要「有电就连接、无需登录」，用 bootstart install --mode task 注册计划任务"
    if name == "linux":
        return (
            "要「有电就连接、无需登录」，用 packaging/campusnet.service 装成系统级单元，"
            "需 root，步骤见 README「开机自启动」一节"
        )
    if name == "darwin":
        return (
            "要「有电就连接、无需登录」，用 packaging/com.campusnet.daemon.plist 装成 "
            "LaunchDaemon，需 root，步骤见 README「开机自启动」一节"
        )
    return ""


def verify_launch() -> tuple[bool, str]:
    """干跑一次「解释器 + 导入 campusnet」，确认自启动真要拉起时不会当场失败。

    只做导入、不启动服务：路径写错、虚拟环境被删、依赖缺失这类
    「重启后才发现」的问题，都能在这里提前暴露。
    """
    frozen = frozen_prefix()
    if frozen is not None:
        ok, message = _run_quiet([frozen[0], "--version"])
        return (True, "") if ok else (False, message or "可执行文件无法运行")

    backend = ROOT / "project" / "backend"
    script = (
        "import sys;"
        f"sys.path.insert(0, {str(backend)!r});"
        "import campusnet;"
        "print(campusnet.__version__)"
    )
    ok, message = _run_quiet([str(python_executable()), "-c", script])
    if ok:
        return True, ""
    return False, message or "解释器或模块导入失败"


def task_action(host: str = API_HOST, port: int = API_PORT) -> tuple[str, str]:
    """计划任务要执行的可执行文件与参数（分开给，PowerShell 需要两段）。"""
    frozen = frozen_prefix()
    if frozen is not None:
        return frozen[0], subprocess.list2cmdline(launch_args(host, port))
    exe = str(python_executable())
    args = subprocess.list2cmdline([str(run_script()), *launch_args(host, port)])
    return exe, args


def _ps_quote(text: str) -> str:
    """PowerShell 单引号字符串的转义：内部单引号写成两个。"""
    return "'" + str(text).replace("'", "''") + "'"


WINDOWS_PS_MODULES = r"C:\Windows\System32\WindowsPowerShell\v1.0\Modules"


def _child_env_with_modules(base: dict[str, str] | None = None) -> dict[str, str]:
    """补上 Windows PowerShell 自带的模块目录。

    执行环境（例如打包好的运行时、或某些 IDE 终端）可能把 PSModulePath
    指向别处，而 powershell.exe 不会再补自己的 $PSHOME\\Modules，
    于是 ScheduledTasks / Microsoft.PowerShell.Security 这些自带模块会加载失败。
    """
    env = dict(os.environ if base is None else base)
    current = env.get("PSModulePath", "")
    if WINDOWS_PS_MODULES.lower() not in current.lower():
        env["PSModulePath"] = (
            f"{current};{WINDOWS_PS_MODULES}" if current else WINDOWS_PS_MODULES
        )
    return env


def _run_powershell_script(
    script: str, env: dict[str, str] | None = None
) -> tuple[bool, str]:
    """跑一段 PowerShell 脚本。

    密码不走命令行（`-Command` 后面的字符串会出现在进程列表里），而是放进子进程
    环境变量；也因此不能用 `-Command -` 从 stdin 读脚本——PowerShell 会把 stdin
    整体当成脚本，密码那一行会被当成命令去执行（这里踩过一次）。
    """
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return False, "找不到 powershell，无法注册计划任务"
    env = _child_env_with_modules(env)
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=60,
            env=env,
            **kwargs,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    output = ((proc.stdout or b"") + (proc.stderr or b"")).decode(
        "utf-8", "replace"
    ).strip()
    return proc.returncode == 0, output


def task_registered() -> bool:
    """计划任务是否已注册。非 Windows 或没有 schtasks 时直接返回 False。"""
    if platform_name() != "windows" or not shutil.which("schtasks"):
        return False
    ok, _ = _run_quiet(["schtasks", "/Query", "/TN", TASK_NAME])
    return ok


def _looks_like_permission_denied(message: str) -> bool:
    """判断 PowerShell 的报错是不是「权限不足」。

    不能只匹配英文 `access is denied`：中文 Windows 回的是「拒绝访问」，
    而项目面向的就是中文用户——那样 UAC 提权提示永远不会出现，
    用户只会看到一串原始报错。HRESULT 0x80070005 是 E_ACCESSDENIED，
    在各种语言下都一样，所以一并认。
    """
    low = (message or "").lower()
    return any(
        token in low
        for token in ("access is denied", "拒绝访问", "access denied", "0x80070005", "accessdenied")
    )


def register_task(host: str, port: int, password: str) -> tuple[bool, str]:
    """注册「开机触发、无论是否登录都运行」的计划任务。

    为什么要密码：Windows 只有两条路能让任务不经登录运行——把密码交给
    任务计划程序保存（它存进 LSA），或者让机器自动登录（密码会落到注册表）。
    这里选前者。密码**不进命令行**（`-Command` 后面的字符串谁都能从进程列表里
    读到），只经子进程环境变量传给 PowerShell；同机同用户的进程理论上仍能读到
    那个变量，但不走 stdin 是有原因的——`-Command -` 会把 stdin 整体当脚本来执行，
    而 `[Console]::In.ReadLine()` 的输入编码在 Windows 上默认是 OEM 代码页，
    非 ASCII 密码会被转坏成错的密码。详见 `_run_powershell_script` 的说明。

    三个容易踩的设置项，都必须显式写死：
    - `-ExecutionTimeLimit 0`：默认上限是 **3 天**，到点任务会被直接终止——
      对一个常驻守护来说等于「跑三天后悄悄消失」，而且只在开机时触发过、
      任务计划程序也不会自己重启它（错误码停在 0x41306）。
    - `-MultipleInstances IgnoreNew`：避免计划任务和桌面快捷方式叠加起两份。
    - `-DontStopOnIdleEnd`：空闲/切电池时不因为默认策略被掐掉。
    """
    if not password:
        return False, "需要当前 Windows 账户的密码，才能注册「无需登录」的任务"
    user = os.environ.get("USERNAME", "").strip()
    domain = os.environ.get("USERDOMAIN", "").strip()
    if not user:
        return False, "读不到当前 Windows 用户名，无法注册计划任务"
    account = f"{domain}\\{user}" if domain else user
    exe, args = task_action(host, port)
    child_env = _child_env_with_modules()
    child_env["CAMPUSNET_TASK_PASSWORD"] = password
    script = (
        "$ErrorActionPreference = 'Stop';"
        # 用 .NET 直接构造 SecureString：不依赖任何需要自动加载的模块，
        # 这样 PSModulePath 被改坏时也不会失败。
        "$sec = New-Object System.Security.SecureString;"
        "foreach ($ch in $env:CAMPUSNET_TASK_PASSWORD.ToCharArray())"
        " { $sec.AppendChar($ch) };"
        "$sec.MakeReadOnly();"
        f"$act = New-ScheduledTaskAction -Execute {_ps_quote(exe)}"
        f" -Argument {_ps_quote(args)};"
        # 开机触发覆盖「有电就来」；登录触发是补网：开机那一刻网卡/无线驱动
        # 常常还没就绪，登录时再来一次能缩短用户的等待。
        "$trg = @("
        " (New-ScheduledTaskTrigger -AtStartup),"
        f" (New-ScheduledTaskTrigger -AtLogOn -User {_ps_quote(account)})"
        ");"
        "$set = New-ScheduledTaskSettingsSet -StartWhenAvailable"
        " -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)"
        " -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries"
        " -DontStopOnIdleEnd"
        " -ExecutionTimeLimit ([TimeSpan]::Zero)"
        " -MultipleInstances IgnoreNew;"
        f"Register-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Action $act"
        # Windows PowerShell 5.1 的参数名是 -User（-UserId 属于 New-ScheduledTaskPrincipal）
        f" -Trigger $trg -Settings $set -User {_ps_quote(account)}"
        " -Password $sec -Force | Out-Null;"
    )
    ok, message = _run_powershell_script(script, child_env)
    if not ok and _looks_like_permission_denied(message):
        return False, (
            "需要管理员权限才能注册「开机触发、无需登录」的任务"
            "（Windows 拒绝为该账户保存凭据）。在本机双击桌面上的「校园网自连」"
            "进菜单选「注册开机自启」，它会主动提出提权重试；也可以在管理员 "
            "PowerShell 里执行：python utils/run.py bootstart install --mode task"
        )
    return ok, message


def delete_task() -> tuple[bool, str]:
    return _run_quiet(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])


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
    frozen = frozen_prefix()
    if frozen is not None:
        # 冻结成单文件后没有 utils/run.py，让 exe 自己 serve。
        return [*frozen, *launch_args(host, port)]
    return [str(python_executable()), str(run_script()), *launch_args(host, port)]


def launch_command_line(args: Sequence[str]) -> str:
    """把一段子命令拼成命令行（自启动脚本与桌面快捷方式共用）。"""
    frozen = frozen_prefix()
    prefix = (
        frozen if frozen is not None else [str(python_executable()), str(run_script())]
    )
    return subprocess.list2cmdline([*prefix, *args])


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
    command = " ".join(
        f'"{item}"' if " " in item else item for item in launch_command(host, port)
    )
    return (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        f'cd /d "{ROOT}"\r\n'
        "set PYTHONIOENCODING=utf-8\r\n"
        "set PYTHONUTF8=1\r\n"
        f'start "" {command}'
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
        f"Exec={' '.join(desktop_arg(arg) for arg in launch_command(host, port))}\n"
        f"Path={ROOT}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def desktop_arg(value: str) -> str:
    """把一个命令行参数按 Desktop Entry 启动项的规则包成 Exec 里的双引号形式。

    与 shlex.quote 不能互换：Desktop Entry 只认双引号（单引号会被当成参数内容的
    一部分），而 shlex.quote 优先给单引号；反过来这里的 `%%` 在 sh 里又是原样。
    shortcuts.py 生成 .desktop 时用的也是这个函数，两边必须是同一份实现。

    转义只做一层。Exec 是 string 类型的值，读的一方按通用规则解一次转义，
    所以参数里字面的一个反斜杠在文件里写成两个；再套一层就变成四个，解出来
    是两个，路径里平白多出一个反斜杠。
    """
    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
    escaped = escaped.replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
    return '"' + escaped + '"'


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
    # 调用方按中文关键词判定失败原因（「拒绝访问」「找不到」），必须按控制台
    # 代码页解码；UTF-8 硬解会把这些词全毁掉，判据永远不成立。
    out = decode_bytes(proc.stdout or b"")
    err = decode_bytes(proc.stderr or b"")
    return proc.returncode == 0, (out + err).strip()


def install(
    host: str = API_HOST,
    port: int = API_PORT,
    mode: str = "auto",
    password: str = "",
) -> BootStatus:
    """注册开机自启动。已存在则覆盖，不报错。

    mode="auto" 用各平台的默认载体（Windows 是启动文件夹，属「登录后」语义）；
    mode="task" 在 Windows 上注册计划任务，做到「开机即运行、无需登录」，
    代价是要把当前账户密码交给任务计划程序保存一次。
    """
    name = platform_name()
    if name not in ("windows", "linux", "darwin"):
        return BootStatus(False, False, None, f"不支持的平台：{name}", False)
    path = entry_path()
    try:
        launch_args(host, port)
    except ValueError as exc:
        return BootStatus(True, False, path, f"启动参数无效：{exc}", False)
    ok, message = verify_launch()
    if not ok:
        # 装了也起不来：宁可现在报失败，也别让用户重启后才发现没连上。
        return BootStatus(True, False, path, f"启动自检失败：{message}", False)
    if mode == "task":
        if name != "windows":
            return BootStatus(
                True,
                False,
                None,
                "计划任务模式目前只支持 Windows，其它平台请用默认载体",
                False,
            )
        ok, message = register_task(host, port, password)
        if not ok:
            return BootStatus(True, False, None, f"注册计划任务失败：{message}", False)
        return BootStatus(
            True,
            True,
            None,
            "已注册计划任务（开机触发，无需登录即运行）",
            True,
            effective=True,
        )
    systemctl = shutil.which("systemctl") if name == "linux" else None
    try:
        if name == "windows":
            _write_entry(path, windows_launcher_text(host, port))
        elif name == "darwin":
            _write_entry(path, macos_plist_text(host, port))
        else:
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
            elif is_wsl():
                # WSL 没有桌面会话，XDG autostart 不会被读取：写了等于没写。
                return BootStatus(
                    True,
                    False,
                    _linux_desktop_path(),
                    "WSL 环境既没有 systemd 也没有桌面会话，自启动项不会被执行；"
                    "如确需自启，请先开启 WSL 的 systemd，再用 systemd --user + linger",
                    False,
                )
            else:
                path = _linux_desktop_path()
                _write_entry(path, xdg_desktop_text(host, port))
    except OSError as exc:
        return BootStatus(True, False, path, f"写入失败：{type(exc).__name__}: {exc}", False)
    effective, note = True, ""
    if name == "linux":
        if systemctl:
            if is_wsl():
                note = "（WSL 的 systemd 需手动开启，且要 linger 才能无人登录启动）"
            else:
                # 用户级单元的天花板就是「登录后启动」，不是没装好。不说清楚，
                # 用户在「重启后停在登录界面」这个场景里会以为已经生效了。
                note = f"（登录后才会拉起；{unattended_hint()}）"
        else:
            effective = has_desktop_session()
            if effective:
                note = f"（登录后才会拉起；{unattended_hint()}）"
            else:
                note = "（当前没有桌面会话，XDG autostart 不会被读取）"
    elif name in ("windows", "darwin"):
        # macOS 原来这里什么都不加：LaunchAgent 明明是登录后才跑，却报成光秃秃的
        # 「已注册开机自启动」，与 verify() 的说法对不上。
        note = f"（登录后才会拉起；{unattended_hint()}）"
    return BootStatus(
        True, True, path, f"已注册开机自启动{note}", True, effective=effective
    )


def uninstall() -> BootStatus:
    """移除开机自启动。没装过也算成功——幂等比报错更符合预期。"""
    name = platform_name()
    if name not in ("windows", "linux", "darwin"):
        return BootStatus(False, False, None, f"不支持的平台：{name}", False)
    task_removed = False
    if name == "windows" and task_registered():
        ok, message = delete_task()
        if not ok:
            return BootStatus(True, True, None, f"删除计划任务失败：{message}", False)
        task_removed = True
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
    if task_removed:
        return BootStatus(True, False, None, "已移除开机自启动的计划任务")
    return BootStatus(True, False, None, "未安装开机自启动")


def status() -> BootStatus:
    """查询当前是否已注册。"""
    name = platform_name()
    if name not in ("windows", "linux", "darwin"):
        return BootStatus(False, False, None, f"不支持的平台：{name}")
    if name == "windows" and task_registered():
        return BootStatus(
            True,
            True,
            None,
            f"已注册计划任务「{TASK_NAME}」（开机触发，无需登录）",
            True,
            effective=True,
        )
    if name == "linux":
        desktop = _linux_desktop_path()
        if desktop.exists():
            effective = has_desktop_session()
            detail = (
                "已注册 XDG 登录自启动"
                if effective
                else "已写入 XDG autostart，但当前环境没有桌面会话，登录时不会被执行"
            )
            return BootStatus(True, True, desktop, detail, True, effective=effective)
        unit = _linux_unit_path()
        if unit.exists():
            systemctl = shutil.which("systemctl")
            if not systemctl:
                return BootStatus(
                    True,
                    False,
                    unit,
                    "单元存在，但无法确认启用状态：未找到 systemctl",
                    True,
                    effective=False,
                )
            ok, message = _run_quiet([systemctl, "--user", "is-enabled", SYSTEMD_UNIT])
            enabled = ok and message.strip() == "enabled"
            detail = (
                f"已启用 systemd 登录自启动；{unattended_hint()}"
                if enabled
                else f"单元未确认启用：{message}"
            )
            return BootStatus(True, enabled, unit, detail, True, effective=enabled)
        hint = "未安装开机自启动"
        if is_wsl() and not shutil.which("systemctl"):
            hint = (
                "未安装；且当前是 WSL 环境（没有 systemd 与桌面会话），"
                "装 XDG autostart 也不会被执行"
            )
        return BootStatus(True, False, _linux_unit_path(), hint, True, effective=False)
    path = entry_path()
    if path.exists():
        detail = "已注册开机自启动"
        if name == "windows":
            detail = f"已注册开机自启动（启动文件夹，登录后才会拉起；{unattended_hint()}）"
        elif name == "darwin":
            # 原来这里只说「已注册开机自启动」，而 verify() 明说 LaunchAgent 只在
            # 图形会话里跑——两处口径不一致，用户会以为重启后无人登录也能连上。
            detail = f"已注册 LaunchAgent（登录后才会运行；{unattended_hint()}）"
        return BootStatus(True, True, path, detail)
    return BootStatus(True, False, path, "未安装开机自启动", True, effective=False)


def verify() -> tuple[bool, str]:
    """自证「无需登录即运行」到底有没有真的生效。

    「已写入」不等于「会生效」——这正是 status() 里 effective 字段要表达的差异，
    但它只能靠静态判断。这里拿运行事实说话：

    - Windows task 模式：对比计划任务的「上次运行时间」与「系统开机时间」，
      任务在开机后跑过，才证明它确实是开机触发、没等任何人登录。
    - Linux systemd：linger 打开后 user 管理器才会在开机时启动并常驻，
      这是「未登录也运行」的前提；否则只是登录时启动。
    - macOS：LaunchDaemon（system 域）才不依赖图形会话，LaunchAgent 做不到。

    返回 (是否已验证生效, 一句话结论)。查不到事实时宁可说「未证实」，不假装成功。
    """
    name = platform_name()
    if name == "windows":
        if not task_registered():
            return False, "未安装「无需登录」的计划任务（先跑 bootstart install --mode task）"
        exe = shutil.which("powershell") or shutil.which("pwsh")
        if not exe:
            return False, "找不到 powershell，无法读取任务运行时间"
        script = (
            "$b = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime;"
            f"$i = Get-ScheduledTaskInfo -TaskName {_ps_quote(TASK_NAME)}"
            " -ErrorAction SilentlyContinue;"
            "if ($i -and $i.LastRunTime) {"
            "  Write-Output ('BOOT=' + $b.ToUniversalTime().ToString('o'));"
            "  Write-Output ('RUN=' + $i.LastRunTime.ToUniversalTime().ToString('o'))"
            "} else { Write-Output 'NOT_RUN' }"
        )
        ok, output = _run_powershell_script(script)
        if not ok or not output.strip():
            return False, "任务已注册，但读取运行记录失败，暂无法自证"
        if "NOT_RUN" in output:
            return False, "任务已注册，但开机后尚未运行过——重启一次后再跑本命令验证"
        boot = run = None
        for line in output.splitlines():
            if line.startswith("BOOT="):
                boot = line[5:].strip()
            elif line.startswith("RUN="):
                run = line[4:].strip()
        if not boot or not run:
            return False, "读取到的时间格式不对，暂无法自证"
        # 时间戳同为 ISO-8601 UTC，字符串比较等价于时间比较。
        if run >= boot:
            return True, "任务在本次开机后已运行——无需登录自启已生效"
        return False, "任务上一次运行早于本次开机——很可能没有按预期触发，请重新安装"

    if name == "linux":
        unit = _linux_unit_path()
        if not unit.exists():
            return False, "未安装 systemd 单元（先跑 bootstart install）"
        loginctl = shutil.which("loginctl")
        if not loginctl:
            return False, "未找到 loginctl，无法确认 linger 状态"
        user = os.environ.get("USER", "") or os.environ.get("USERNAME", "")
        ok, out = _run_quiet([loginctl, "show-user", user, "-p", "Linger"])
        linger_on = ok and "Linger=yes" in out
        if linger_on:
            return True, "systemd user 已启用 linger——未登录时也会在开机后启动"
        return False, "systemd 单元已装，但 linger 未开启（登录后才启动）——用 loginctl enable-linger 开启"

    if name == "darwin":
        plist = _macos_plist_path()
        if plist.exists():
            # LaunchAgent 写在 ~/Library/LaunchAgents，只在图形会话里跑；
            # 真正的「未登录也运行」要 /Library/LaunchDaemons + root。这里如实说明差异。
            return False, (
                "已安装 LaunchAgent（登录后才会运行）；"
                "要做到未登录也运行需改用 LaunchDaemon 并以 root 安装"
            )
        return False, "未安装自启动（先跑 bootstart install）"

    return False, f"不支持的平台：{name}"


def activate(status_result: BootStatus) -> str:
    """把结果翻成一句给用户看的话，避免调用方各写一遍。"""
    suffix = f"（{status_result.path}）" if status_result.path else ""
    if not status_result.success:
        return f"开机自启动操作失败：{status_result.detail}{suffix}"
    if not status_result.supported:
        return f"当前系统不支持自动配置开机自启动：{status_result.detail}"
    if status_result.installed:
        if not status_result.effective:
            return (
                f"开机自启动已写入，但在本机不会生效：{status_result.detail}{suffix}"
            )
        return f"开机自启动已开启{suffix}"
    if status_result.path:
        return f"开机自启动未开启：{status_result.detail}{suffix}"
    return f"开机自启动未开启：{status_result.detail}"
