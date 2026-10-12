"""路径与运行时常量。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _repo_root() -> Path:
    # campusnet/paths.py -> campusnet -> backend -> project -> 仓库根
    return Path(__file__).resolve().parents[3]


def data_home(home: Path) -> Path:
    """冻结运行时的数据目录（数据库、日志、密钥都落这里）。

    开发时不需要它：仓库目录本身就是可写的。之所以单独一份逻辑，是因为
    PyInstaller 单文件运行时资源在临时解包目录里——把数据库写在那儿，
    每次运行都会丢配置。
    """
    override = os.environ.get("CAMPUSNET_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        return (Path(base) if base else home / "AppData" / "Local") / "campusnet"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "campusnet"
    return home / ".local" / "share" / "campusnet"


def resolve_roots(
    *, frozen: bool, bundle: Path, source: Path, home: Path | None = None
) -> tuple[Path, Path]:
    """返回 (资源根, 数据根)，抽成纯函数便于直接测冻结模式。"""
    if not frozen:
        return source, source
    return bundle, data_home(home or Path.home())


IS_FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_ROOT, ROOT = resolve_roots(
    frozen=IS_FROZEN,
    bundle=Path(getattr(sys, "_MEIPASS", _repo_root())),
    source=_repo_root(),
)
BACKEND_DIR = RESOURCE_ROOT / "project" / "backend"
FRONTEND_DIR = RESOURCE_ROOT / "project" / "frontend"
UTILS_DIR = RESOURCE_ROOT / "utils"
DOC_DIR = RESOURCE_ROOT / "doc"
DATABASE_DIR = ROOT / "database"
def db_path() -> Path:
    """解析数据库路径。

    必须在调用时读环境变量：如果在模块导入时求值成常量，
    之后设置的 CAMPUSNET_DB 就完全失效，测试与多实例隔离都做不到。
    """
    override = os.environ.get("CAMPUSNET_DB")
    if override:
        return Path(override)
    return DATABASE_DIR / "campusnet.db"


def api_token_path() -> Path:
    """局域网控制台令牌路径；与数据库同目录，便于备份与权限管理。"""
    override = os.environ.get("CAMPUSNET_API_TOKEN_FILE")
    return Path(override).expanduser() if override else db_path().with_suffix(".api-token")


def instance_lock_path() -> Path:
    """服务单实例锁路径；默认与当前数据库放在同一目录。"""
    override = os.environ.get("CAMPUSNET_INSTANCE_LOCK_FILE")
    return Path(override).expanduser() if override else db_path().with_suffix(".lock")


# 兼容旧引用：导入时的快照，仅供不便改动调用方时使用。
# 新代码请调 db_path()，否则环境变量改不动。
DB_PATH = db_path()
# 建表脚本是只读资源：冻结后它在解包目录里，而数据库在数据目录里。
SCHEMA_PATH = RESOURCE_ROOT / "database" / "schema.sql"

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
    # 只保证「要写的地方」存在：资源目录在冻结运行时是解包出来的，不该也不需要在里面建目录。
    for path in (DATABASE_DIR, DATABASE_DIR / "logs"):
        path.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def frozen_prefix() -> list[str] | None:
    """冻结运行时返回 [exe]；开发环境返回 None（调用方回落到解释器 + run.py）。"""
    return [sys.executable] if IS_FROZEN else None


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
