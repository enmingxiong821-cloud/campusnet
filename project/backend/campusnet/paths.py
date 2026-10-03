"""路径与运行时常量。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _repo_root() -> Path:
    # campusnet/paths.py -> campusnet -> backend -> project -> 仓库根
    return Path(__file__).resolve().parents[3]


ROOT = _repo_root()
BACKEND_DIR = ROOT / "project" / "backend"
FRONTEND_DIR = ROOT / "project" / "frontend"
UTILS_DIR = ROOT / "utils"
DOC_DIR = ROOT / "doc"
DATABASE_DIR = ROOT / "database"
DB_PATH = Path(os.environ.get("CAMPUSNET_DB", DATABASE_DIR / "campusnet.db"))
SCHEMA_PATH = DATABASE_DIR / "schema.sql"

# 后端 API 端口（2001-3000），前端静态端口（1001-2000）
API_PORT = int(os.environ.get("CAMPUSNET_API_PORT", "2268"))
WEB_PORT = int(os.environ.get("CAMPUSNET_WEB_PORT", "1268"))
API_HOST = os.environ.get("CAMPUSNET_API_HOST", "127.0.0.1")
WEB_HOST = os.environ.get("CAMPUSNET_WEB_HOST", "127.0.0.1")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def ensure_dirs() -> None:
    for path in (DOC_DIR, FRONTEND_DIR, BACKEND_DIR, DATABASE_DIR, UTILS_DIR):
        path.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def stdout_ready() -> None:
    """Windows 控制台默认可能是 GBK，这里统一成 UTF-8 容错输出。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass