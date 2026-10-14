"""存储层单元测试。"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest
from campusnet import storage as storage_mod

from campusnet.storage import (
    STATE_FAILED,
    STATE_ONLINE,
    Account,
    Device,
    Store,
    WakePlan,
    accounts_to_rows,
)


def test_add_and_get_account(store: Store, account: Account) -> None:
    new_id = store.add_account(account)
    assert new_id > 0

    loaded = store.get_account(new_id)
    assert loaded is not None
    assert loaded.name == "宿舍"
    assert loaded.username == "20240001"
    assert loaded.password == "p@ssw0rd"
    assert loaded.enabled is True


def test_password_not_exposed_in_public(account: Account) -> None:
    account.password = "topsecret"
    assert "topsecret" not in str(account.public())


def test_update_account_keeps_password_when_blank(
    store: Store, account: Account
) -> None:
    new_id = store.add_account(account)
    changed = store.get_account(new_id)
    assert changed is not None
    changed.name = "改过的名字"
    changed.password = ""
    changed.password = "p@ssw0rd"  # 服务端在 payload 为空时保留旧密码
    assert store.update_account(new_id, changed)

    again = store.get_account(new_id)
    assert again is not None and again.name == "改过的名字"


def test_extra_fields_roundtrip(store: Store) -> None:
    acc = Account(
        name="drcom",
        portal_url="http://10.0.0.1/login",
        username="u",
        password="p",
        extra_fields={"0MKKey": "1", "hiddenPwd": ""},
    )
    new_id = store.add_account(acc)
    loaded = store.get_account(new_id)
    assert loaded is not None
    assert loaded.extra_fields == {"0MKKey": "1", "hiddenPwd": ""}


def test_delete_and_missing_get(store: Store, account: Account) -> None:
    new_id = store.add_account(account)
    assert store.delete_account(new_id) is True
    assert store.get_account(new_id) is None
    assert store.delete_account(new_id) is False


def test_list_accounts_enabled_filter(store: Store) -> None:
    on = Account(
        name="开", portal_url="http://a", username="u", password="p", enabled=True
    )
    off = Account(
        name="关", portal_url="http://b", username="u", password="p", enabled=False
    )
    store.add_account(on)
    store.add_account(off)

    assert len(store.list_accounts()) == 2
    enabled_only = store.list_accounts(enabled_only=True)
    assert len(enabled_only) == 1 and enabled_only[0].name == "开"


def test_set_state_updates_last_checked(store: Store, account: Account) -> None:
    new_id = store.add_account(account)
    store.set_state(new_id, STATE_ONLINE)
    loaded = store.get_account(new_id)
    assert loaded is not None
    assert loaded.last_state == STATE_ONLINE
    assert loaded.last_checked_at
    assert loaded.last_ok_at == loaded.last_checked_at
    store.set_state(new_id, STATE_FAILED)
    assert store.get_account(new_id).last_ok_at == loaded.last_ok_at


def test_events_order_and_filter(store: Store, account: Account) -> None:
    acc_id = store.add_account(account)
    store.add_event("第一条", account_id=acc_id)
    store.add_event("第二条", account_id=acc_id, level="warning")
    store.add_event("别的账号", account_id=None)

    events = store.list_events(limit=10)
    assert [e["message"] for e in events][:2] == ["别的账号", "第二条"]
    assert events[0]["account_id"] is None

    scoped = store.list_events(limit=10, account_id=acc_id)
    assert {e["message"] for e in scoped} == {"第一条", "第二条"}
    assert scoped[0]["message"] == "第二条"  # DESC


def test_clear_events(store: Store) -> None:
    store.add_event("x")
    store.clear_events()
    assert store.list_events() == []


def test_settings_roundtrip(store: Store) -> None:
    store.set_setting("interval", 45)
    store.set_setting("headless", False)
    store.set_setting("autostart", True)

    assert store.get_setting("interval") == 45
    assert store.get_setting("headless") is False
    assert store.get_setting("missing", "fallback") == "fallback"

    settings = store.all_settings()
    assert settings["interval"] == 45 and settings["autostart"] is True


def test_reinit_db_is_idempotent(tmp_db) -> None:  # noqa: ANN001
    first = Store(tmp_db)
    first.add_account(
        Account(name="x", portal_url="http://a", username="u", password="p")
    )
    second = Store(tmp_db)
    assert len(second.list_accounts()) == 1


def test_accounts_to_rows(store: Store, account: Account) -> None:
    store.add_account(account)
    rows = accounts_to_rows(store.list_accounts())
    assert rows and "password" not in rows[0]


def test_failed_state_roundtrip(store: Store, account: Account) -> None:
    new_id = store.add_account(account)
    store.set_state(new_id, STATE_FAILED)
    loaded = store.get_account(new_id)
    assert loaded is not None and loaded.last_state == STATE_FAILED


def test_connection_commits_and_closes(store: Store) -> None:
    with store._connection() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)", ("commit_test", "42")
        )
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    assert store.get_setting("commit_test") == 42


def test_connection_rolls_back_and_closes(store: Store) -> None:
    conn: sqlite3.Connection | None = None
    with pytest.raises(RuntimeError, match="abort transaction"):
        with store._connection() as current:
            conn = current
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                ("rollback_test", "42"),
            )
            raise RuntimeError("abort transaction")
    assert conn is not None
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    assert store.get_setting("rollback_test") is None


def test_store_operations_close_connections(
    store: Store, account: Account, monkeypatch
) -> None:
    opened = []
    original = store.connect

    def tracked_connect():
        conn = original()
        opened.append(conn)
        return conn

    monkeypatch.setattr(store, "connect", tracked_connect)
    store.init_db()
    account_id = store.add_account(account)
    store.list_accounts()
    store.get_account(account_id)
    store.update_account(account_id, account)
    store.set_state(account_id, STATE_ONLINE)
    event_id = store.add_event("connection test", account_id=account_id)
    store.list_events()
    store.event_count()
    store.event_exists(event_id)
    store.prune_events()
    store.clear_events()
    store.set_setting("connection_test", True)
    store.get_setting("connection_test")
    store.all_settings()
    store.delete_account(account_id)
    assert opened
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def _legacy_database(path):
    schema = storage_mod.SCHEMA_PATH.read_text(encoding="utf-8")
    schema = schema.replace("    last_ok_at        TEXT,\n", "")
    with sqlite3.connect(path) as conn:
        conn.executescript(schema)
        conn.execute("INSERT INTO settings VALUES ('legacy', '42')")
        conn.execute(
            "INSERT INTO accounts (name, portal_url, username, password_obf) VALUES (?, ?, ?, ?)",
            ("legacy", "http://portal.invalid", "synthetic", ""),
        )


def test_legacy_schema_migrates_without_data_loss(tmp_path):
    path = tmp_path / "legacy.db"
    _legacy_database(path)
    migrated = Store(path)
    assert migrated.get_setting("legacy") == 42
    assert migrated.list_accounts()[0].last_ok_at is None
    assert Store(path).list_accounts()[0].name == "legacy"
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0]
            == storage_mod.SCHEMA_VERSION
        )
        assert [r[1] for r in conn.execute("PRAGMA table_info(accounts)")].count(
            "last_ok_at"
        ) == 1


def test_migration_failure_rolls_back_schema_and_version(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    _legacy_database(path)
    original = Store._migrate_v2

    def fail(conn):
        original(conn)
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(Store, "_migrate_v2", staticmethod(fail))
    with pytest.raises(RuntimeError, match="injected"):
        Store(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "last_ok_at" not in {
            r[1] for r in conn.execute("PRAGMA table_info(accounts)")
        }
        assert (
            conn.execute("SELECT value FROM settings WHERE key='legacy'").fetchone()[0]
            == "42"
        )


def test_newer_database_version_is_rejected(tmp_path):
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 999")
    with pytest.raises(RuntimeError, match="拒绝降级"):
        Store(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 999
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            == []
        )


def test_wake_plan_roundtrip(store: Store) -> None:
    new_id = store.add_wake_plan(
        WakePlan(name="宿舍台式机", mac="AA:BB:CC:DD:EE:FF", schedule="07:30")
    )

    plans = store.list_wake_plans()
    assert [p.id for p in plans] == [new_id]
    loaded = plans[0]
    assert loaded.name == "宿舍台式机"
    assert loaded.mac == "AA:BB:CC:DD:EE:FF"
    assert loaded.enabled is True
    assert loaded.last_run_at is None


def test_wake_plan_delete_and_mark_run(store: Store) -> None:
    from datetime import datetime

    new_id = store.add_wake_plan(
        WakePlan(name="p", mac="AA:BB:CC:DD:EE:FF", schedule="07:30")
    )

    store.mark_wake_plan_run(new_id, datetime(2026, 10, 13, 7, 30))
    loaded = store.list_wake_plans()[0]
    assert loaded.last_run_at == "2026-10-13 07:30:00"

    assert store.delete_wake_plan(new_id) is True
    assert store.delete_wake_plan(new_id) is False
    assert store.list_wake_plans() == []


def test_device_roundtrip_and_delete(store: Store) -> None:
    new_id = store.add_device(
        Device(name="树莓派", mac="11:22:33:44:55:66", note="宿舍跳板")
    )

    devices = store.list_devices()
    assert devices[0].id == new_id
    assert devices[0].name == "树莓派"
    assert devices[0].note == "宿舍跳板"

    assert store.delete_device(new_id) is True
    assert store.list_devices() == []


def test_setting_string_roundtrip_keeps_type(store: Store) -> None:
    """字符串恰好是合法 JSON 时也要读回字符串，不能被 json.loads 改成 int/bool/None。"""
    store.set_setting("note", "123")
    store.set_setting("flag", "true")
    store.set_setting("blank", "null")

    assert store.get_setting("note") == "123"
    assert store.get_setting("flag") == "true"
    assert store.get_setting("blank") == "null"


@pytest.mark.skipif(os.name != "posix", reason="Windows 不看 POSIX 权限位")
def test_store_restricts_db_permissions_to_0600(tmp_db) -> None:  # noqa: ANN001
    """库里存着加密后的账号密码与通知密钥，库文件（含 WAL 旁挂文件）要收紧到 0600。"""
    Store(tmp_db)

    assert stat.S_IMODE(tmp_db.stat().st_mode) == 0o600
    for suffix in ("-wal", "-shm"):
        side = Path(f"{tmp_db}{suffix}")
        if side.exists():  # 旁挂文件按访问时序出现，存在就必须是 0600
            assert stat.S_IMODE(side.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="Windows 不看 POSIX 权限位")
def test_restrict_permissions_swallows_oserror(tmp_db, monkeypatch) -> None:  # noqa: ANN001
    """收紧权限失败（只读挂载、权限不足）不能让服务起不来。"""

    def boom(path, mode):  # noqa: ANN001
        raise OSError("只读挂载")

    monkeypatch.setattr(storage_mod.os, "chmod", boom)
    Store(tmp_db)  # 不该抛
