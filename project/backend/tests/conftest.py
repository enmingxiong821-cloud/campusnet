"""pytest 共享夹具：隔离数据库、临时目录。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from campusnet.storage import Account, Store  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "test.db"
    monkeypatch.setenv("CAMPUSNET_DB", str(db))
    monkeypatch.setenv("CAMPUSNET_OBF_KEY", "unit-test-obf-key")
    return db


@pytest.fixture()
def store(tmp_db: Path) -> Store:
    return Store(tmp_db)


@pytest.fixture()
def account() -> Account:
    return Account(
        name="宿舍",
        portal_url="http://10.0.0.1/eportal/portal/login",
        username="20240001",
        password="p@ssw0rd",
    )