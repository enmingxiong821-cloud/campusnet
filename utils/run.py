"""Windows/Linux 通用启动入口：自行处理 sys.path，避免中文路径下 PYTHONPATH 失效。"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "project" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from campusnet.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())