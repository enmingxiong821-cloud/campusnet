-- 校园网自连 SQLite 结构
-- 位置由 project/backend/campusnet/paths.py 计算，默认 database/campusnet.db

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS accounts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,
    portal_url        TEXT    NOT NULL,
    probe_url         TEXT    NOT NULL DEFAULT 'http://www.msftconnecttest.com/connecttest.txt',
    username          TEXT    NOT NULL,
    password_obf      TEXT    NOT NULL,
    username_field    TEXT    NOT NULL DEFAULT '',
    password_field    TEXT    NOT NULL DEFAULT '',
    password_mode     TEXT    NOT NULL DEFAULT 'plain',
    login_mode        TEXT    NOT NULL DEFAULT 'http',
    extra_fields      TEXT    NOT NULL DEFAULT '{}',
    submit_button     TEXT    NOT NULL DEFAULT '',
    max_retries       INTEGER NOT NULL DEFAULT 3,
    enabled           INTEGER NOT NULL DEFAULT 1,
    last_state        TEXT    NOT NULL DEFAULT 'unknown',
    last_checked_at   TEXT,
    last_ok_at        TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    account_id  INTEGER,
    level       TEXT    NOT NULL DEFAULT 'info',
    stage       TEXT    NOT NULL DEFAULT '',
    state       TEXT    NOT NULL DEFAULT '',
    message     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wake_plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    mac         TEXT    NOT NULL,
    broadcast   TEXT    NOT NULL DEFAULT '255.255.255.255',
    port        INTEGER NOT NULL DEFAULT 9,
    schedule    TEXT    NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS devices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    mac         TEXT    NOT NULL,
    note        TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_events_ts     ON events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_acc    ON events (account_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_accounts_name ON accounts (name);
