"""提交前钩子（F5）：锁住「本地扫敏感串」这条防线。

钩子脚本只是薄封装，真正的规则在 utils/check_distribution.py（那边已有单测）。
这里锁两件事：钩子确实调用了分发扫描、且当前仓库扫描干净——避免有人把钩子
改成空跑，或随手把敏感文件混进仓库后钩子形同虚设。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HOOK = ROOT / "utils" / "pre-commit"
SCANNER = ROOT / "utils" / "check_distribution.py"


def test_hook_invokes_distribution_scanner() -> None:
    text = HOOK.read_text(encoding="utf-8")
    assert "check_distribution.py" in text
    assert "--repo" in text
    # 钩子必须拦下命中（非零退出），而不是扫完就放行。
    assert "exit 1" in text


def test_repo_is_clean_under_distribution_scan() -> None:
    """当前仓库扫描必须干净：钩子才能在本地与 CI 上一致放行。"""
    proc = subprocess.run(
        [sys.executable, str(SCANNER), "--repo", str(ROOT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"check_distribution.py --repo 退出码 {proc.returncode}\n"
        f"stdout:\n{proc.stdout[-1500:]}\nstderr:\n{proc.stderr[-1500:]}"
    )
