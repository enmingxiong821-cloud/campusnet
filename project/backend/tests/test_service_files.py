"""系统级服务文件（E4）：锁住它们不和程序自己的启动命令漂移。

`packaging/campusnet.service` 与 `packaging/com.campusnet.daemon.plist` 是
给「开机即连、不等登录」用的系统级配置，而 `bootstart` 生成的是用户级单元
（登录后才起）。两份东西各写各的，就一定会有一份悄悄过期——这里只锁真正
要紧的三件事：

  1. 启动参数就是 `bootstart.launch_args()`，别处改了默认 host/port，
     服务文件也跟着红；
  2. 注册目标是「机器起来」而不是「用户登录」（multi-user / LaunchDaemon）；
  3. 换行是 LF。这两个文件要在 Linux/macOS 上被解析，而本仓库在 Windows 上
     开发且开了 core.autocrlf，CR 落到 unit 文件里会被当成值的一部分。

顺带守住「不以 root 跑」：控制台是有令牌认证的网络入口，以 root 跑等于把
这个入口的任何绕过放大成整机沦陷。真要远程关机，走 polkit 最小授权。
"""

from __future__ import annotations

import plistlib
from pathlib import Path

from campusnet.bootstart import LAUNCH_AGENT_LABEL, launch_args

REPO_ROOT = Path(__file__).resolve().parents[3]
UNIT = REPO_ROOT / "packaging" / "campusnet.service"
PLIST = REPO_ROOT / "packaging" / "com.campusnet.daemon.plist"

#: 默认 host/port 下的启动子命令，服务文件里的启动参数必须与它逐字相同。
EXPECTED_ARGS = launch_args()


def _unit_entries(text: str) -> dict[tuple[str, str], list[str]]:
    """按 systemd 的方式读 unit 文件。

    不用 configparser：`Environment=` 会重复出现，而 configparser 默认在
    重复键上直接抛错（strict=False 又只留最后一个值，会把编码变量吃掉）。
    """
    entries: dict[tuple[str, str], list[str]] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        entries.setdefault((section, key.strip()), []).append(value.strip())
    return entries


def _single(entries: dict[tuple[str, str], list[str]], section: str, key: str) -> str:
    values = entries[(section, key)]
    assert len(values) == 1, f"[{section}] {key} 应恰好出现一次，实为 {values}"
    return values[0]


def _exec_tail(command: str) -> list[str]:
    return command.split()[-len(EXPECTED_ARGS) :]


def test_systemd_unit_starts_the_same_command() -> None:
    entries = _unit_entries(UNIT.read_text(encoding="utf-8"))
    assert _exec_tail(_single(entries, "Service", "ExecStart")) == EXPECTED_ARGS


def test_systemd_unit_registers_for_boot_not_login() -> None:
    entries = _unit_entries(UNIT.read_text(encoding="utf-8"))
    # multi-user.target 才是「机器起来」；default.target 是用户级单元的写法，
    # 抄错这一行会让服务退化成「登录后才自启」，而安装步骤看起来完全正常。
    assert _single(entries, "Install", "WantedBy") == "multi-user.target"
    # 认证要真的能出网，网卡 up 但还没拿到地址时启动等于白跑第一次检测。
    assert "network-online.target" in _single(entries, "Unit", "After")


def test_systemd_unit_runs_unprivileged_and_restarts_on_failure() -> None:
    entries = _unit_entries(UNIT.read_text(encoding="utf-8"))
    assert _single(entries, "Service", "User") == "campusnet"
    assert _single(entries, "Service", "User") != "root"
    # on-failure 而不是 always：单次认证失败由监控循环重试，不该算进程退出。
    assert _single(entries, "Service", "Restart") == "on-failure"


def test_systemd_unit_pins_utf8_output() -> None:
    entries = _unit_entries(UNIT.read_text(encoding="utf-8"))
    assert "PYTHONIOENCODING=utf-8" in entries[("Service", "Environment")]


def test_launchdaemon_starts_the_same_command() -> None:
    data = plistlib.loads(PLIST.read_bytes())
    assert data["ProgramArguments"][-len(EXPECTED_ARGS) :] == EXPECTED_ARGS


def test_launchdaemon_registers_for_boot_not_login() -> None:
    data = plistlib.loads(PLIST.read_bytes())
    # 和登录时拉起的那个 LaunchAgent 必须用不同的 Label，否则两份会互相顶掉。
    assert data["Label"] == "com.campusnet.daemon"
    assert data["Label"] != LAUNCH_AGENT_LABEL
    assert data["RunAtLoad"] is True
    # 与 systemd 单元的 Restart=on-failure 对齐：正常退出不管，异常退出才拉起。
    assert data["KeepAlive"] == {"SuccessfulExit": False}


def test_launchdaemon_runs_unprivileged_and_pins_utf8() -> None:
    data = plistlib.loads(PLIST.read_bytes())
    assert data["UserName"] == "campusnet"
    assert data["UserName"] != "root"
    assert data["EnvironmentVariables"]["PYTHONIOENCODING"] == "utf-8"


def test_service_files_use_lf_endings() -> None:
    for path in (UNIT, PLIST):
        assert b"\r" not in path.read_bytes(), f"{path.name} 含 CR，应为 LF"
