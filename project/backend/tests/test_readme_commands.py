"""README 命令冒烟测试（F6）：锁住「README 里的命令真实可用」。

文档里的命令会漂移：A1 就曾因 README 示例 `docs/openapi.json` 写错路径而翻车。
这里把 README 里所有 `python utils/run.py <子命令>` 抽出来，逐个跑 `--help`
断言退出码 0——子命令改名、删掉，或写成不存在的命令，都会当场变红，
不用等用户照着做才炸。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"
RUN_PY = REPO_ROOT / "utils" / "run.py"

# 抽 `python utils/run.py <子命令>` 的第一个词（子命令名）。命令带多行续行或
# 一堆参数都不影响——我们只关心子命令名，`--help` 由它自己解析。
_CMD_RE = re.compile(r"python\s+utils/run\.py\s+([a-z][a-z0-9-]*)")


def _readme_subcommands() -> set[str]:
    text = README.read_text(encoding="utf-8")
    return {match.group(1) for match in _CMD_RE.finditer(text)}


def _run_help(sub: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # argparse 的 --help 会直接退出、不走到 stdout_ready()，Windows 下默认 GBK；
    # 统一 UTF-8，避免断言信息里解码炸掉，CI（ubuntu）本就是 UTF-8。
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(RUN_PY), sub, "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env=env,
    )


def test_readme_lists_expected_commands() -> None:
    """防呆：正则失效或 README 被清空时，抽取结果不能缩水成空集。"""
    subs = _readme_subcommands()
    assert {"serve", "doctor", "add", "check"} <= subs, subs


def test_every_readme_command_help_exits_zero() -> None:
    """README 里每个 `python utils/run.py <子命令>` 都能 `--help` 成功退出。"""
    subs = sorted(_readme_subcommands())
    assert subs, "README 里没抽到任何 `python utils/run.py <子命令>`"
    for sub in subs:
        proc = _run_help(sub)
        assert proc.returncode == 0, (
            f"`python utils/run.py {sub} --help` 退出码 {proc.returncode}\n"
            f"stdout:\n{proc.stdout[-500:]}\nstderr:\n{proc.stderr[-500:]}"
        )
