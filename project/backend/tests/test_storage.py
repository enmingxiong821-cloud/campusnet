"""存储层单元测试。"""

from __future__ import annotations

import sqlite3

import pytest

from campusnet.storage import (
    STATE_FAILED,
    STATE_ONLINE,
    Account,
    Store,
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


def test_update_account_keeps_password_when_blank(store: Store, account: Account) -> None:
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
    on = Account(name="开", portal_url="http://a", username="u", password="p", enabled=True)
    off = Account(name="关", portal_url="http://b", username="u", password="p", enabled=False)
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
    first.add_account(Account(name="x", portal_url="http://a", username="u", password="p"))
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
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("commit_test", "42"))
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    assert store.get_setting("commit_test") == 42


def test_connection_rolls_back_and_closes(store: Store) -> None:
    conn: sqlite3.Connection | None = None
    with pytest.raises(RuntimeError, match="abort transaction"):
        with store._connection() as current:
            conn = current
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("rollback_test", "42"))
            raise RuntimeError("abort transaction")
    assert conn is not None
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    assert store.get_setting("rollback_test") is None


def test_store_operations_close_connections(store: Store, account: Account, monkeypatch) -> None:
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
