"""SQLite 存储层：账号、事件日志、配置项。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import crypto
from .paths import SCHEMA_PATH, db_path as db_path_default

_WRITE_LOCK = threading.Lock()
SCHEMA_VERSION = 4

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
    last_ok_at: str | None = None

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
            "last_ok_at": self.last_ok_at,
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
            last_ok_at=row["last_ok_at"],
        )


@dataclass
class WakePlan:
    """唤醒计划：每天/每周几点发一个 WOL 魔术包到指定 MAC。"""

    id: int = 0
    name: str = ""
    mac: str = ""
    broadcast: str = "255.255.255.255"
    port: int = 9
    schedule: str = ""
    enabled: bool = True
    last_run_at: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "mac": self.mac,
            "broadcast": self.broadcast,
            "port": self.port,
            "schedule": self.schedule,
            "enabled": self.enabled,
            "last_run_at": self.last_run_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WakePlan":
        return cls(
            id=row["id"],
            name=row["name"],
            mac=row["mac"],
            broadcast=row["broadcast"],
            port=row["port"],
            schedule=row["schedule"],
            enabled=bool(row["enabled"]),
            last_run_at=row["last_run_at"],
        )


@dataclass
class Device:
    """可唤醒设备清单：名称 + MAC + 备注，一键发 WOL。"""

    id: int = 0
    name: str = ""
    mac: str = ""
    note: str = ""
    created_at: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "mac": self.mac,
            "note": self.note,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Device":
        return cls(
            id=row["id"],
            name=row["name"],
            mac=row["mac"],
            note=row["note"] or "",
            created_at=row["created_at"],
        )


class Store:
    """SQLite 存储。默认路径在实例化时才解析，改环境变量能立刻生效。"""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else db_path_default()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()
        self._restrict_permissions()

    def _restrict_permissions(self) -> None:
        """库文件收紧到 0600。

        库里存着账号密码（加密后）与通知 Webhook 的密钥，而 sqlite 建文件用的是
        umask（通常 0644）——同机其他用户读得到。加密密钥文件本来就是 0600 建出来的
        （见 crypto），库却一直敞着。WAL 模式下还有 -wal / -shm 两个旁挂文件，
        正文会落在那里，一并收紧。Windows 不看 POSIX 权限位，跳过。
        """
        if os.name != "posix":
            return
        for suffix in ("", "-wal", "-shm"):
            target = Path(f"{self.db_path}{suffix}")
            try:
                if target.exists():
                    os.chmod(target, 0o600)
            except OSError:  # 只读挂载、权限不足等：收紧失败不该让服务起不来
                pass

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            conn.close()
            raise
        # -wal / -shm 会在最后一个连接干净关闭时被 SQLite 删掉，下一次打开又以
        # umask（通常 0644）重建——只在 __init__ 收一次权限不够，重建出来的旁挂
        # 文件里落着事件正文与 settings，同机其他用户读得到。每次建连接补一次。
        self._restrict_permissions()
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """提交或回滚事务后立即关闭连接，不依赖垃圾回收。"""
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init_db(self) -> None:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        with _WRITE_LOCK, self._connection() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库版本 {version} 高于当前支持的 {SCHEMA_VERSION}，拒绝降级运行"
                )
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("BEGIN IMMEDIATE")
            # 获得写锁后重读，覆盖独立进程并发初始化的情形。
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库版本 {version} 高于当前支持的 {SCHEMA_VERSION}，拒绝降级运行"
                )
            statement = ""
            for line in schema.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    if "PRAGMA journal_mode" not in statement:
                        conn.execute(statement)
                    statement = ""
            if statement.strip():
                raise RuntimeError("数据库结构脚本包含未结束的 SQL")
            if version < 2:
                self._migrate_v2(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _migrate_v2(conn: sqlite3.Connection) -> None:
        """旧库新增事实字段；不从当前状态推断未知的历史成功时间。"""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
        if "last_ok_at" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN last_ok_at TEXT")

    # ---------- accounts ----------
    def list_accounts(self, enabled_only: bool = False) -> list[Account]:
        sql = "SELECT * FROM accounts"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        with self._connection() as conn:
            return [Account.from_row(r) for r in conn.execute(sql)]

    def get_account(self, account_id: int) -> Account | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        return Account.from_row(row) if row else None

    def add_account(self, account: Account) -> int:
        with _WRITE_LOCK, self._connection() as conn:
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
        with _WRITE_LOCK, self._connection() as conn:
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
        with _WRITE_LOCK, self._connection() as conn:
            cur = conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            return cur.rowcount > 0

    def set_state(self, account_id: int, state: str) -> None:
        with _WRITE_LOCK, self._connection() as conn:
            conn.execute(
                """
                UPDATE accounts SET last_state = ?, last_checked_at = datetime('now','localtime'),
                       last_ok_at = CASE WHEN ? IN ('online', 'logged_in')
                           THEN datetime('now','localtime') ELSE last_ok_at END
                 WHERE id = ?
                """,
                (state, state, account_id),
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
        with _WRITE_LOCK, self._connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO events (account_id, level, stage, state, message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (account_id, level, stage, state, message),
            )
            rowid = cur.lastrowid
            return int(rowid) if rowid is not None else 0

    def list_events(
        self, limit: int = 200, account_id: int | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events"
        params: list[Any] = []
        if account_id is not None:
            sql += " WHERE account_id = ?"
            params.append(account_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 2000)))
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def clear_events(self) -> None:
        with _WRITE_LOCK, self._connection() as conn:
            conn.execute("DELETE FROM events")

    def prune_events(self, keep: int = 2000) -> int:
        """只保留最近 keep 条，避免监控长期运行把库撑爆。

        监控默认 60 秒一轮，每轮至少写一条事件，一年下来就是几十万行。
        """
        keep = max(1, int(keep))
        with _WRITE_LOCK, self._connection() as conn:
            cur = conn.execute(
                "DELETE FROM events WHERE id NOT IN "
                "(SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                (keep,),
            )
            return max(0, cur.rowcount or 0)

    def event_count(self) -> int:
        with self._connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return int(row["n"]) if row else 0

    def event_exists(self, event_id: int) -> bool:
        """确认某条事件还在表里。

        裁剪限频要用它兜底：记过一次公告后，后续轮次的裁剪会把那条
        公告本身删掉，若只看时间戳就永远不会补记。
        """
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM events WHERE id = ? LIMIT 1", (event_id,)
            ).fetchone()
        return row is not None

    # ---------- settings ----------
    def set_setting(self, key: str, value: Any) -> None:
        # 一律走 json：裸存字符串时 get_setting 的 json.loads 会把恰好合法的
        # "123"/"true"/"null" 读成 int/bool/None，类型往返就丢了。
        blob = json.dumps(value, ensure_ascii=False)
        with _WRITE_LOCK, self._connection() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, blob),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def all_settings(self) -> dict[str, Any]:
        with self._connection() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                out[row["key"]] = row["value"]
        return out

    # ---------- wake plans ----------
    def list_wake_plans(self, enabled_only: bool = False) -> list[WakePlan]:
        sql = "SELECT * FROM wake_plans"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        with self._connection() as conn:
            return [WakePlan.from_row(r) for r in conn.execute(sql)]

    def add_wake_plan(self, plan: WakePlan) -> int:
        with _WRITE_LOCK, self._connection() as conn:
            cur = conn.execute(
                "INSERT INTO wake_plans (name, mac, broadcast, port, schedule, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    plan.name,
                    plan.mac,
                    plan.broadcast,
                    plan.port,
                    plan.schedule,
                    1 if plan.enabled else 0,
                ),
            )
            rowid = cur.lastrowid
            return int(rowid) if rowid is not None else 0

    def delete_wake_plan(self, plan_id: int) -> bool:
        with _WRITE_LOCK, self._connection() as conn:
            cur = conn.execute("DELETE FROM wake_plans WHERE id = ?", (plan_id,))
            return cur.rowcount > 0

    def mark_wake_plan_run(self, plan_id: int, when: datetime) -> None:
        with _WRITE_LOCK, self._connection() as conn:
            conn.execute(
                "UPDATE wake_plans SET last_run_at = ? WHERE id = ?",
                (when.strftime("%Y-%m-%d %H:%M:%S"), plan_id),
            )

    # ---------- devices ----------
    def list_devices(self) -> list[Device]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM devices ORDER BY id").fetchall()
        return [Device.from_row(r) for r in rows]

    def add_device(self, device: Device) -> int:
        with _WRITE_LOCK, self._connection() as conn:
            cur = conn.execute(
                "INSERT INTO devices (name, mac, note) VALUES (?, ?, ?)",
                (device.name, device.mac, device.note),
            )
            rowid = cur.lastrowid
            return int(rowid) if rowid is not None else 0

    def delete_device(self, device_id: int) -> bool:
        with _WRITE_LOCK, self._connection() as conn:
            cur = conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
            return cur.rowcount > 0

    # ---------- pair codes ----------
    def add_pair_code(self, code_hash: str, expires_at: str) -> None:
        """存配对码哈希（不存明文）。expires_at 形如 YYYY-MM-DD HH:MM:SS。"""
        with _WRITE_LOCK, self._connection() as conn:
            conn.execute(
                "INSERT INTO pair_codes (code_hash, expires_at) VALUES (?, ?)",
                (code_hash, expires_at),
            )

    def get_pair_code(self, code_hash: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM pair_codes WHERE code_hash = ?", (code_hash,)
            ).fetchone()
        return dict(row) if row else None

    def mark_pair_code_used(self, code_hash: str, user_agent: str) -> bool:
        """领取配对码，返回是否领到。

        `used_at IS NULL` 写在 WHERE 里，并发请求只有一个能改到行——先查后写会在
        两个请求之间留出窗口，同码被双领，「单次使用」就只是句空话了。
        """
        with _WRITE_LOCK, self._connection() as conn:
            cur = conn.execute(
                "UPDATE pair_codes SET used_at = datetime('now','localtime'), "
                "user_agent = ? WHERE code_hash = ? AND used_at IS NULL",
                (user_agent, code_hash),
            )
            return (cur.rowcount or 0) > 0

    def record_pair_failure(self, code_hash: str) -> int:
        """记一次失败并返回当前失败次数（用于 3 次锁定）。"""
        with _WRITE_LOCK, self._connection() as conn:
            conn.execute(
                "UPDATE pair_codes SET failures = failures + 1 WHERE code_hash = ?",
                (code_hash,),
            )
            row = conn.execute(
                "SELECT failures FROM pair_codes WHERE code_hash = ?", (code_hash,)
            ).fetchone()
        return int(row["failures"]) if row else 0

    def purge_pair_codes(self) -> int:
        """删除过期配对码，避免表无限增长。"""
        with _WRITE_LOCK, self._connection() as conn:
            cur = conn.execute(
                "DELETE FROM pair_codes WHERE expires_at < datetime('now','localtime')"
            )
            return cur.rowcount or 0


def accounts_to_rows(accounts: Iterable[Account]) -> list[dict[str, Any]]:
    return [a.public() for a in accounts]
