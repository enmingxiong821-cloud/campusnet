"""可移植凭据：封套加解密、导出/导入语义、CLI 往返。"""

from __future__ import annotations

import json
import os
import stat

import pytest

from campusnet import cli as cli_mod
from campusnet import credentials as cred_mod
from campusnet import crypto
from campusnet.storage import Account, Store

#: 测试里把 PBKDF2 迭代降到 1000，避免每个用例都做 20 万次哈希。
FAST = 1000


# ---------- 封套本身 ----------


def test_blob_round_trip() -> None:
    envelope = crypto.encrypt_blob(b"hello", "pw", iterations=FAST)

    assert envelope["kdf"] == crypto.EXPORT_KDF
    assert crypto.decrypt_blob(envelope, "pw") == b"hello"


def test_blob_rejects_wrong_passphrase() -> None:
    envelope = crypto.encrypt_blob(b"hello", "pw", iterations=FAST)

    with pytest.raises(crypto.CredentialExportError, match="口令不对"):
        crypto.decrypt_blob(envelope, "other")


def test_blob_rejects_tampered_body() -> None:
    envelope = crypto.encrypt_blob(b"hello", "pw", iterations=FAST)
    raw = bytearray(crypto._unb64(str(envelope["body"])))
    raw[-1] ^= 0x01
    envelope["body"] = crypto._b64(bytes(raw))

    with pytest.raises(crypto.CredentialExportError, match="口令不对"):
        crypto.decrypt_blob(envelope, "pw")


def test_blob_rejects_unknown_version() -> None:
    envelope = crypto.encrypt_blob(b"hello", "pw", iterations=FAST)
    envelope["version"] = 99

    with pytest.raises(crypto.CredentialExportError, match="版本或算法"):
        crypto.decrypt_blob(envelope, "pw")


def test_blob_rejects_empty_passphrase() -> None:
    with pytest.raises(crypto.CredentialExportError, match="不能为空"):
        crypto.encrypt_blob(b"x", "")


# ---------- 账号导出/导入 ----------


def _two_accounts(store: Store) -> None:
    store.add_account(
        Account(
            name="宿舍",
            portal_url="http://10.0.0.1/eportal/portal/login",
            probe_url="http://probe/1",
            username="20240001",
            password="p@ss",
            password_mode="base64",
            login_mode="http",
            max_retries=5,
        )
    )
    store.add_account(
        Account(
            name="实验室",
            portal_url="http://10.0.0.2/srun_portal_pc",
            username="20240002",
            password="另一个口令",
            login_mode="srun",
            extra_fields={"ac_id": "1"},
        )
    )


def test_export_import_round_trip(tmp_path) -> None:  # noqa: ANN001
    source = Store(tmp_path / "source.db")
    _two_accounts(source)
    path = cred_mod.write_export(
        cred_mod.export_accounts(source, "迁移口令", iterations=FAST),
        tmp_path / "creds.json",
    )

    restored = Store(tmp_path / "target.db")
    report = cred_mod.import_accounts(restored, cred_mod.read_export(path), "迁移口令")

    assert (report.created, report.updated, report.skipped) == (2, 0, 0)
    by_name = {item.name: item for item in restored.list_accounts()}
    assert by_name["宿舍"].password == "p@ss"
    assert by_name["宿舍"].max_retries == 5
    assert by_name["宿舍"].password_mode == "base64"
    assert by_name["实验室"].login_mode == "srun"
    assert by_name["实验室"].extra_fields == {"ac_id": "1"}
    # 源账号没配探测地址时，导入要回落到默认探活地址而不是留空
    assert by_name["实验室"].probe_url


def test_import_is_idempotent(tmp_path) -> None:  # noqa: ANN001
    source = Store(tmp_path / "a.db")
    _two_accounts(source)
    envelope = cred_mod.export_accounts(source, "pw", iterations=FAST)
    target = Store(tmp_path / "b.db")

    first = cred_mod.import_accounts(target, envelope, "pw")
    second = cred_mod.import_accounts(target, envelope, "pw")

    assert (first.created, first.updated) == (2, 0)
    assert (second.created, second.updated) == (0, 2)
    assert len(target.list_accounts()) == 2


def test_import_overwrites_changed_password(tmp_path) -> None:  # noqa: ANN001
    source = Store(tmp_path / "a.db")
    _two_accounts(source)
    target = Store(tmp_path / "b.db")
    cred_mod.import_accounts(
        target, cred_mod.export_accounts(source, "pw", iterations=FAST), "pw"
    )

    changed = source.list_accounts()[0]
    changed.password = "改过的口令"
    source.update_account(changed.id, changed)

    report = cred_mod.import_accounts(
        target, cred_mod.export_accounts(source, "pw", iterations=FAST), "pw"
    )

    assert report.updated == 2
    assert "改过的口令" in {item.password for item in target.list_accounts()}


def test_dry_run_does_not_write(tmp_path) -> None:  # noqa: ANN001
    source = Store(tmp_path / "a.db")
    _two_accounts(source)
    target = Store(tmp_path / "b.db")

    report = cred_mod.import_accounts(
        target,
        cred_mod.export_accounts(source, "pw", iterations=FAST),
        "pw",
        dry_run=True,
    )

    assert report.created == 2
    assert "试运行" in report.summary()
    assert target.list_accounts() == []


def test_read_export_rejects_foreign_file(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "other.json"
    path.write_text(json.dumps({"kind": "something-else"}), encoding="utf-8")

    with pytest.raises(cred_mod.CredentialFileError, match="不是校园网自连"):
        cred_mod.read_export(path)


def test_read_export_rejects_broken_json(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(cred_mod.CredentialFileError, match="不是合法 JSON"):
        cred_mod.read_export(path)


def test_read_export_rejects_missing_file(tmp_path) -> None:  # noqa: ANN001
    with pytest.raises(cred_mod.CredentialFileError, match="读不到导出文件"):
        cred_mod.read_export(tmp_path / "nope.json")


@pytest.mark.skipif(os.name != "posix", reason="Windows 权限由 ACL 管理")
def test_export_file_is_private(tmp_path) -> None:  # noqa: ANN001
    source = Store(tmp_path / "a.db")
    _two_accounts(source)

    path = cred_mod.write_export(
        cred_mod.export_accounts(source, "pw", iterations=FAST), tmp_path / "c.json"
    )

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ---------- CLI ----------


def test_cli_cred_round_trip(tmp_path, monkeypatch, capsys) -> None:  # noqa: ANN001
    source_db = tmp_path / "cli-source.db"
    monkeypatch.setenv("CAMPUSNET_DB", str(source_db))
    _two_accounts(Store(source_db))
    secret = tmp_path / "pass.txt"
    secret.write_text("迁移口令\n", encoding="utf-8")
    out = tmp_path / "creds.json"

    code = cli_mod.main(
        ["cred", "export", "--out", str(out), "--passphrase-file", str(secret)]
    )
    assert code == 0
    assert "已导出 2 个账号" in capsys.readouterr().out
    assert out.exists()

    target_db = tmp_path / "cli-target.db"
    monkeypatch.setenv("CAMPUSNET_DB", str(target_db))
    code = cli_mod.main(
        ["cred", "import", "--file", str(out), "--passphrase-file", str(secret)]
    )

    assert code == 0
    assert "新增 2 个" in capsys.readouterr().out
    assert len(Store(target_db).list_accounts()) == 2


def test_cli_cred_import_wrong_passphrase(tmp_path, monkeypatch, capsys) -> None:  # noqa: ANN001
    store = Store(tmp_path / "s.db")
    _two_accounts(store)
    out = cred_mod.write_export(
        cred_mod.export_accounts(store, "right", iterations=FAST), tmp_path / "c.json"
    )
    monkeypatch.setenv("CAMPUSNET_DB", str(tmp_path / "target.db"))
    bad = tmp_path / "bad.txt"
    bad.write_text("wrong\n", encoding="utf-8")

    code = cli_mod.main(
        ["cred", "import", "--file", str(out), "--passphrase-file", str(bad)]
    )

    assert code == 2
    assert "口令不对" in capsys.readouterr().err


def test_cli_cred_export_without_accounts(tmp_path, monkeypatch, capsys) -> None:  # noqa: ANN001
    monkeypatch.setenv("CAMPUSNET_DB", str(tmp_path / "empty.db"))
    secret = tmp_path / "p.txt"
    secret.write_text("pw\n", encoding="utf-8")

    code = cli_mod.main(
        [
            "cred",
            "export",
            "--out",
            str(tmp_path / "o.json"),
            "--passphrase-file",
            str(secret),
        ]
    )

    assert code == 2
    assert "没什么可导出的" in capsys.readouterr().out
