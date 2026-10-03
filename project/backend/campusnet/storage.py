"""SQLite 存储层：账号、事件日志、配置项。"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import crypto
from .paths import DB_PATH, SCHEMA_PATH

_WRITE_LOCK = threading.Lock()

STATE_UNKNOWN = "unknown"
STATE_ONLINE = "online"
STATE_OFFLINE = "offline"
STATE_LOGGED_IN = "logged_in"
STATE_FAILED = "failed"


@dataclass
class Account:
    id: int = 0
    name: str = ""
    portal_url: str = ""
    probe_url: str = "http://www.msftconnecttest.com/connecttest.txt"
    username: str = ""
    password: str = field(default="", repr=False)
    username_field: str = ""
    password_field: str = ""
    password_mode: str = "plain"
    login_mode: str = "http"
    extra_fields: dict[str, str] = field(default_factory=dict)
    submit_button: str = ""
    max_retries: int = 3
    enabled: bool = True
    last_state: str = STATE_UNKNOWN
    last_checked_at: str | None = None

    @property
    def password_encoded(self) -> str:
        return crypto.encode_password(self.password, self.password_mode)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "portal_url": self.portal_url,
            "probe_url": self.probe_url,
            "username": self.username,
            "username_field": self.username_field,
            "password_field": self.password_field,
            "password_mode": self.password_mode,
            "login_mode": self.login_mode,
            "extra_fields": self.extra_fields,
            "submit_button": self.submit_button,
            "max_retries": self.max_retries,
            "enabled": self.enabled,
            "last_state": self.last_state,
            "last_checked_at": self.last_checked_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Account":
        try:
            extra = json.loads(row["extra_fields"] or "{}")
        except json.JSONDecodeError:
            extra = {}
        return cls(
            id=row["id"],
            name=row["name"],
            portal_url=row["portal_url"],
            probe_url=row["probe_url"],
            username=row["username"],
            password=crypto.deobfuscate(row["password_obf"]),
            username_field=row["username_field"],
            password_field=row["password_field"],
            password_mode=row["password_mode"],
            login_mode=row["login_mode"],
            extra_fields=extra if isinstance(extra, dict) else {},
            submit_button=row["submit_button"],
            max_retries=row["max_retries"],
            enabled=bool(row["enabled"]),
            last_state=row["last_state"],
            last_checked_at=row["last_checked_at"],
        )


class Store:
    def __init__(self, db_path: Path | str = DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        with _WRITE_LOCK, self.connect() as conn:
            conn.executescript(schema)

    # ---------- accounts ----------
    def list_accounts(self, enabled_only: bool = False) -> list[Account]:
        sql = "SELECT * FROM accounts"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        with self.connect() as conn:
            return [Account.from_row(r) for r in conn.execute(sql)]

    def get_account(self, account_id: int) -> Account | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return Account.from_row(row) if row else None

    def add_account(self, account: Account) -> int:
        with _WRITE_LOCK, self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO accounts (name, portal_url, probe_url, username, password_obf,
                                      username_field, password_field, password_mode, login_mode,
                                      extra_fields, submit_button, max_retries, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account.name,
                    account.portal_url,
                    account.probe_url,
                    account.username,
                    crypto.obfuscate(account.password),
                    account.username_field,
                    account.password_field,
                    account.password_mode,
                    account.login_mode,
                    json.dumps(account.extra_fields, ensure_ascii=False),
                    account.submit_button,
                    account.max_retries,
                    1 if account.enabled else 0,
                ),
            )
            rowid = cur.lastrowid
            return int(rowid) if rowid is not None else 0

    def update_account(self, account_id: int, account: Account) -> bool:
        with _WRITE_LOCK, self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE accounts SET name = ?, portal_url = ?, probe_url = ?, username = ?,
                       password_obf = ?, username_field = ?, password_field = ?,
                       password_mode = ?, login_mode = ?, extra_fields = ?,
                       submit_button = ?, max_retries = ?, enabled = ?,
                       updated_at = datetime('now','localtime')
                 WHERE id = ?
                """,
                (
                    account.name,
                    account.portal_url,
                    account.probe_url,
                    account.username,
                    crypto.obfuscate(account.password),
                    account.username_field,
                    account.password_field,
                    account.password_mode,
                    account.login_mode,
                    json.dumps(account.extra_fields, ensure_ascii=False),
                    account.submit_button,
                    account.max_retries,
                    1 if account.enabled else 0,
                    account_id,
                ),
            )
            return cur.rowcount > 0

    def delete_account(self, account_id: int) -> bool:
        with _WRITE_LOCK, self.connect() as conn:
            cur = conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            return cur.rowcount > 0

    def set_state(self, account_id: int, state: str) -> None:
        with _WRITE_LOCK, self.connect() as conn:
            conn.execute(
                """
                UPDATE accounts SET last_state = ?, last_checked_at = datetime('now','localtime')
                 WHERE id = ?
                """,
                (state, account_id),
            )

    # ---------- events ----------
    def add_event(
        self,
        message: str,
        level: str = "info",
        stage: str = "",
        state: str = "",
        account_id: int | None = None,
    ) -> int:
        with _WRITE_LOCK, self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO events (account_id, level, stage, state, message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (account_id, level, stage, state, message),
            )
            rowid = cur.lastrowid
            return int(rowid) if rowid is not None else 0

    def list_events(self, limit: int = 200, account_id: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events"
        params: list[Any] = []
        if account_id is not None:
            sql += " WHERE account_id = ?"
            params.append(account_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 2000)))
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def clear_events(self) -> None:
        with _WRITE_LOCK, self.connect() as conn:
            conn.execute("DELETE FROM events")

    def prune_events(self, keep: int = 2000) -> int:
        """只保留最近 keep 条，避免监控长期运行把库撑爆。

        监控默认 60 秒一轮，每轮至少写一条事件，一年下来就是几十万行。
        """
        keep = max(1, int(keep))
        with _WRITE_LOCK, self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM events WHERE id NOT IN "
                "(SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                (keep,),
            )
            return max(0, cur.rowcount or 0)

    def event_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return int(row["n"]) if row else 0

    # ---------- settings ----------
    def set_setting(self, key: str, value: Any) -> None:
        blob = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        with _WRITE_LOCK, self.connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, blob),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def all_settings(self) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                out[row["key"]] = row["value"]
        return out


def accounts_to_rows(accounts: Iterable[Account]) -> list[dict[str, Any]]:
    return [a.public() for a in accounts]