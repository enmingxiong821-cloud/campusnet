"""为每种用法生成桌面快捷方式，省掉记命令。

Windows 生成 .lnk（PowerShell + WScript.Shell），macOS/Linux 生成可执行脚本。
交互式命令一律包一层「保留窗口」：双击后窗口一闪而过、看不到报错，
是这类工具最常见的劝退点。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .bootstart import platform_name, python_executable
from .paths import ROOT


@dataclass(frozen=True)
class Shortcut:
    name: str
    args: tuple[str, ...]
    hint: str = ""


#: 覆盖现在的每一种用法；新增子命令后在这里加一行即可。
#: 桌面只留一个入口：打开菜单，其余操作在里面选。
#: 注册开机自启需要管理员权限，菜单会主动提出提权重试，所以不必再单独开一个快捷方式。
DEFAULT_SHORTCUTS: tuple[Shortcut, ...] = (
    Shortcut("校园网自连", ("menu",), "启动服务、打开控制台、检测、自检、开关机都在里面"),
)

#: 早期版本生成过的快捷方式，重新生成时顺手清掉，避免桌面越攒越多。
LEGACY_NAMES: tuple[str, ...] = (
    "启动校园网服务",
    "打开控制台",
    "立即检测网络",
    "查看自检报告",
    "查看多端访问",
    "查看运行日志",
    "远程关机",
    "远程重启",
    "注册开机自启-无需登录",
    "注册开机自启-无需登录(需管理员)",
)


def run_script() -> Path:
    return ROOT / "utils" / "run.py"


def desktop_dir() -> Path:
    if os.name == "nt":
        profile = os.environ.get("USERPROFILE", "").strip()
        base = Path(profile) if profile else Path.home()
        return base / "Desktop"
    return Path.home() / "Desktop"


def _remove_legacy(directory: Path) -> list[str]:
    """删掉早期版本留下的快捷方式（只删我们自己生成的那几个名字）。"""
    removed: list[str] = []
    for name in LEGACY_NAMES:
        for suffix in (".lnk", ".desktop", ".command"):
            target = directory / f"{name}{suffix}"
            if target.exists():
                try:
                    target.unlink()
                    removed.append(target.name)
                except OSError:
                    pass
    return removed


def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _run_powershell(script: str) -> tuple[bool, str]:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return False, "找不到 powershell，无法创建 Windows 快捷方式"
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=60,
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    output = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
    if proc.returncode != 0:
        return False, output.strip() or f"powershell 退出码 {proc.returncode}"
    return True, ""


def create_windows(shortcuts: tuple[Shortcut, ...], directory: Path) -> tuple[bool, str]:
    """在当前用户桌面生成 .lnk；用 cmd /k 包一层，双击后能看到输出。"""
    python = str(python_executable())
    script = [
        "$ErrorActionPreference = 'Stop';",
        "$W = New-Object -ComObject WScript.Shell;",
        f"$dir = {_ps_quote(str(directory))};",
        "New-Item -ItemType Directory -Force -Path $dir | Out-Null;",
    ]
    for item in shortcuts:
        command = subprocess.list2cmdline(
            [python, str(run_script()), *item.args]
        )
        script.append(
            "$s = $W.CreateShortcut([IO.Path]::Combine($dir, "
            + _ps_quote(item.name + ".lnk")
            + "));"
            + "$s.TargetPath = "
            + _ps_quote("cmd.exe")
            + ";$s.Arguments = "
            + _ps_quote("/k " + command)
            + ";$s.WorkingDirectory = "
            + _ps_quote(str(ROOT))
            + ";$s.Description = "
            + _ps_quote(item.hint or item.name)
            + ";$s.IconLocation = "
            + _ps_quote(python)
            + ";$s.Save();"
        )
    ok, message = _run_powershell("".join(script))
    if not ok:
        return False, message
    return True, f"已创建 {len(shortcuts)} 个快捷方式：{directory}"


def create_windows_elevated(
    shortcuts: tuple[Shortcut, ...], directory: Path
) -> tuple[bool, str]:
    """生成会自我提权的快捷方式（双击弹 UAC），用于必须管理员权限的动作。"""
    python = str(python_executable())
    script = [
        "$ErrorActionPreference = 'Stop';",
        "$W = New-Object -ComObject WScript.Shell;",
        f"$dir = {_ps_quote(str(directory))};",
        "New-Item -ItemType Directory -Force -Path $dir | Out-Null;",
    ]
    for item in shortcuts:
        command = subprocess.list2cmdline([python, str(run_script()), *item.args])
        inner = (
            "Start-Process -Verb RunAs -FilePath 'cmd.exe' -ArgumentList "
            + _ps_quote("/k " + command)
        )
        script.append(
            "$s = $W.CreateShortcut([IO.Path]::Combine($dir, "
            + _ps_quote(item.name + ".lnk")
            + "));"
            + "$s.TargetPath = "
            + _ps_quote("powershell.exe")
            + ";$s.Arguments = "
            + _ps_quote("-NoProfile -Command " + inner)
            + ";$s.WorkingDirectory = "
            + _ps_quote(str(ROOT))
            + ";$s.Description = "
            + _ps_quote(item.hint or item.name)
            + ";$s.Save();"
        )
    ok, message = _run_powershell("".join(script))
    if not ok:
        return False, message
    return True, f"已创建 {len(shortcuts)} 个需管理员的快捷方式：{directory}"


def create_posix(
    shortcuts: tuple[Shortcut, ...], directory: Path, *, darwin: bool
) -> tuple[bool, str]:
    directory.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    for item in shortcuts:
        command = " ".join(
            shlex.quote(part)
            for part in [str(python_executable()), str(run_script()), *item.args]
        )
        if darwin:
            path = directory / f"{item.name}.command"
            path.write_text(
                f"#!/bin/sh\ncd {shlex.quote(str(ROOT))}\n{command}\n",
                encoding="utf-8",
            )
        else:
            path = directory / f"{item.name}.desktop"
            path.write_text(
                "[Desktop Entry]\n"
                "Type=Application\n"
                f"Name={item.name}\n"
                f"Comment={item.hint or item.name}\n"
                f"Exec={command}\n"
                f"Path={ROOT}\n"
                "Terminal=true\n",
                encoding="utf-8",
            )
        path.chmod(0o755)
        created.append(path.name)
    return True, f"已创建 {len(created)} 个快捷方式：{directory}"


def create(
    directory: Path | str | None = None, *, platform: str | None = None
) -> tuple[bool, str]:
    """生成快捷方式；platform 参数供测试注入。"""
    name = platform or platform_name()
    target = Path(directory).expanduser() if directory else desktop_dir()
    removed = _remove_legacy(target)
    if name == "windows":
        ok, message = create_windows(DEFAULT_SHORTCUTS, target)
        if ok and removed:
            message += f"（顺手清理了 {len(removed)} 个旧快捷方式）"
        return ok, message
    if name in ("darwin", "linux"):
        ok, message = create_posix(DEFAULT_SHORTCUTS, target, darwin=name == "darwin")
        if ok and removed:
            message += f"（顺手清理了 {len(removed)} 个旧快捷方式）"
        return ok, message
    return False, f"不支持的平台：{name}"


def _selftest() -> None:  # pragma: no cover - 仅用于人工确认
    print(create(None))


if __name__ == "__main__":  # pragma: no cover
    _selftest()
    sys.exit(0)
