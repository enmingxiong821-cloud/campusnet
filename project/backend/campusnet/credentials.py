"""账号凭据的可移植导出/导入。

为什么需要它：Windows 上密码走 DPAPI，绑定当前用户与这台机器；备份
`database/campusnet.db` 并不能在重装系统或换机后恢复密码。这里把账号
（含明文密码）打包成只受口令保护的封套，用于迁移。

导入策略是「同名 + 同账号覆盖，其余新增」，重复导入不会产生重复账号。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import crypto
from .storage import Account

KIND = "campusnet-credentials"

#: 导出包含的字段；last_state / last_checked_at 这类运行态刻意不带走。
_FIELDS = (
    "name",
    "portal_url",
    "probe_url",
    "username",
    "password",
    "username_field",
    "password_field",
    "password_mode",
    "login_mode",
    "extra_fields",
    "submit_button",
    "max_retries",
    "enabled",
)

ExportError = crypto.CredentialExportError


class CredentialFileError(ExportError):
    """导出文件本身有问题：读不到、不是 JSON、不是本程序的封套。"""


@dataclass
class ImportReport:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    dry_run: bool = False

    def summary(self) -> str:
        prefix = "（试运行，未写入数据库）" if self.dry_run else ""
        return (
            f"{prefix}新增 {self.created} 个，更新 {self.updated} 个，"
            f"跳过 {self.skipped} 个。"
        )


def _record(account: Account) -> dict[str, Any]:
    return {name: getattr(account, name) for name in _FIELDS}


def export_accounts(
    store: Any, passphrase: str, *, iterations: int = crypto.EXPORT_ITERATIONS
) -> dict[str, Any]:
    """把库里的账号（含明文密码）打包成口令保护的封套。"""
    payload = json.dumps(
        {
            "kind": KIND,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "accounts": [_record(item) for item in store.list_accounts()],
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    envelope = crypto.encrypt_blob(payload, passphrase, iterations=iterations)
    envelope["kind"] = KIND
    return envelope


def write_export(envelope: dict[str, Any], path: Path | str) -> Path:
    """原子写入，并在 POSIX 上收紧到 0600——它等价于一份明文口令。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp = target.with_name(f".{target.name}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        with os.fdopen(os.open(temp, flags, 0o600), "w", encoding="utf-8") as stream:
            stream.write(text)
        temp.replace(target)
        if os.name == "posix":
            os.chmod(target, 0o600)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass
    return target


def read_export(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise CredentialFileError(f"读不到导出文件：{exc}") from exc
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CredentialFileError(f"导出文件不是合法 JSON：{exc}") from exc
    if not isinstance(envelope, dict) or envelope.get("kind") != KIND:
        raise CredentialFileError("这不是校园网自连的凭据导出文件")
    return envelope


def import_accounts(
    store: Any,
    envelope: dict[str, Any],
    passphrase: str,
    *,
    dry_run: bool = False,
) -> ImportReport:
    plain = crypto.decrypt_blob(envelope, passphrase)
    try:
        data = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialFileError(f"导出内容解析失败：{exc}") from exc
    records = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(records, list):
        raise CredentialFileError("导出内容里没有账号列表")

    existing = {
        (item.name, item.username): item.id for item in store.list_accounts()
    }
    report = ImportReport(dry_run=dry_run)
    for item in records:
        account = _account_from_record(item)
        if account is None:
            report.skipped += 1
            continue
        key = (account.name, account.username)
        target = existing.get(key)
        if target is None:
            report.created += 1
            if not dry_run:
                existing[key] = store.add_account(account)
        else:
            report.updated += 1
            if not dry_run:
                account.id = target
                store.update_account(target, account)
    return report


def _account_from_record(item: object) -> Account | None:
    if not isinstance(item, dict):
        return None
    try:
        name = str(item.get("name", "")).strip()
        portal_url = str(item.get("portal_url", "")).strip()
        username = str(item.get("username", "")).strip()
        if not name or not portal_url or not username:
            return None
        probe_url = str(item.get("probe_url") or "").strip() or Account.probe_url
        account = Account(
            name=name,
            portal_url=portal_url,
            probe_url=probe_url,
            username=username,
            password=str(item.get("password", "")),
            username_field=str(item.get("username_field", "")),
            password_field=str(item.get("password_field", "")),
            password_mode=str(item.get("password_mode") or "plain"),
            login_mode=str(item.get("login_mode") or "http"),
            submit_button=str(item.get("submit_button", "")),
            max_retries=int(item.get("max_retries") or 3),
            enabled=bool(item.get("enabled", True)),
        )
    except (TypeError, ValueError):
        return None
    extra = item.get("extra_fields")
    account.extra_fields = (
        {str(key): str(value) for key, value in extra.items()}
        if isinstance(extra, dict)
        else {}
    )
    return account
