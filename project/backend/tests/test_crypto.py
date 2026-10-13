"""密码编码与本地混淆的单元测试。"""

from __future__ import annotations

import base64
import hashlib

import pytest

from campusnet import crypto


@pytest.mark.parametrize(
    "password,mode,expected",
    [
        ("secret", "plain", "secret"),
        ("secret", "base64", base64.b64encode(b"secret").decode()),
        ("secret", "md5", hashlib.md5(b"secret").hexdigest()),
        (
            "secret",
            "md5_base64",
            base64.b64encode(hashlib.md5(b"secret").hexdigest().encode()).decode(),
        ),
        ("secret", "sha256", hashlib.sha256(b"secret").hexdigest()),
    ],
)
def test_encode_password_matches_known_values(password: str, mode: str, expected: str) -> None:
    assert crypto.encode_password(password, mode) == expected


def test_encode_password_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="未知的密码编码方式"):
        crypto.encode_password("x", "rot13")


def test_decode_base64_roundtrip() -> None:
    encoded = crypto.encode_password("中文密码", "base64")
    assert crypto.decode_password(encoded, "base64") == "中文密码"


def test_decode_plain_is_identity() -> None:
    assert crypto.decode_password("Abc123!", "plain") == "Abc123!"


def test_obfuscate_roundtrip() -> None:
    token = crypto.obfuscate("Campus@2024")
    assert "Campus@2024" not in token
    assert crypto.deobfuscate(token) == "Campus@2024"


def test_obfuscate_unicode_and_empty() -> None:
    assert crypto.deobfuscate(crypto.obfuscate("密码🎓")) == "密码🎓"
    assert crypto.deobfuscate(crypto.obfuscate("")) == ""


def test_deobfuscate_rejects_garbage() -> None:
    assert crypto.deobfuscate("not-a-token") == ""
    assert crypto.deobfuscate("v9.abc.def") == ""


def test_obfuscated_value_is_stable_per_call() -> None:
    a, b = crypto.obfuscate("same"), crypto.obfuscate("same")
    assert crypto.deobfuscate(a) == crypto.deobfuscate(b) == "same"


# ---------------------------------------------- 平台与损坏输入分支（F2）
# 这些分支在 Windows 与 Linux 上各走一半（DPAPI vs 本地密钥文件），所以测试
# 主动把两种环境都构造出来，覆盖率才不会随跑测平台漂移。


@pytest.fixture()
def posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 os.name 伪装成 posix：让「非 Windows」分支在 Windows 上也能测。

    同时固定混淆密钥，避免测试往真实用户目录写 ~/.campusnet_obf.key。
    """
    monkeypatch.setattr(crypto.os, "name", "posix")
    monkeypatch.setenv("CAMPUSNET_OBF_KEY", "unit-test-obf-key")


@pytest.fixture()
def no_dpapi(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 DPAPI 不可用，强制走本地密钥文件那条封装路径。"""
    monkeypatch.setattr(crypto, "_dpapi_protect", lambda raw: None)
    monkeypatch.setattr(crypto, "_dpapi_unprotect", lambda raw: None)


def test_obfuscate_falls_back_to_local_key(posix, no_dpapi) -> None:
    token = crypto.obfuscate("Campus@2024")
    assert token.startswith("v2.")
    assert crypto.deobfuscate(token) == "Campus@2024"


def test_v2_token_rejects_tampered_mac(posix, no_dpapi) -> None:
    token = crypto.obfuscate("secret")
    prefix, payload, mac = token.split(".", 2)
    # 动一位 MAC 就必须解不出来（HMAC 校验拦住）
    mangled = base64.urlsafe_b64encode(b"\x00" * 32).decode("ascii")
    assert crypto.deobfuscate(f"{prefix}.{payload}.{mangled}") == ""


def test_v2_token_rejects_short_payload(posix, no_dpapi) -> None:
    short = base64.urlsafe_b64encode(b"abc").decode("ascii")
    assert crypto.deobfuscate(f"v2.{short}.{short}") == ""


def test_v3_token_with_bad_base64() -> None:
    """v3 前缀但正文不是合法 base64：直接判失败，不抛异常。"""
    assert crypto.deobfuscate("v3.dpapi.！！！") == ""


def test_v1_token_still_decodes(posix) -> None:
    """v1 老令牌把密钥带在值里，换机也能解——保留兼容。"""
    key = crypto.secrets.token_bytes(32)
    raw = b"legacy-password"
    stream = crypto._keystream(key, len(raw))
    blob = base64.urlsafe_b64encode(
        bytes(a ^ b for a, b in zip(raw, stream))
    ).decode("ascii")
    key_b64 = base64.urlsafe_b64encode(key).decode("ascii")

    assert crypto.deobfuscate(f"v1.{key_b64}.{blob}") == "legacy-password"


def test_dpapi_helpers_return_none_off_windows(posix) -> None:
    assert crypto._dpapi_protect(b"x") is None
    assert crypto._dpapi_unprotect(b"x") is None


def test_local_key_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAMPUSNET_OBF_KEY", "unit-test-key")
    assert crypto._local_key() == hashlib.sha256(b"unit-test-key").digest()


def test_local_key_reads_existing_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # noqa: ANN001
    key_file = tmp_path / "obf.key"
    key = crypto.secrets.token_bytes(32)
    key_file.write_text(
        base64.urlsafe_b64encode(key).decode("ascii"), encoding="utf-8"
    )
    monkeypatch.delenv("CAMPUSNET_OBF_KEY", raising=False)
    monkeypatch.setenv("CAMPUSNET_KEY_FILE", str(key_file))

    assert crypto._local_key() == key


def test_local_key_creates_file_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # noqa: ANN001
    key_file = tmp_path / "sub" / "obf.key"
    monkeypatch.delenv("CAMPUSNET_OBF_KEY", raising=False)
    monkeypatch.setenv("CAMPUSNET_KEY_FILE", str(key_file))

    key = crypto._local_key()

    assert len(key) == 32
    assert key_file.exists()
    # 第二次调用读回同一个密钥（否则重启后旧密码解不开）
    assert crypto._local_key() == key


def test_local_key_survives_unwritable_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # noqa: ANN001
    """密钥文件写不下去也要能用——本次会话内随机密钥，不抛异常。"""
    monkeypatch.delenv("CAMPUSNET_OBF_KEY", raising=False)
    monkeypatch.setenv("CAMPUSNET_KEY_FILE", str(tmp_path / "obf.key"))

    def boom(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(crypto.Path, "write_text", boom)

    assert len(crypto._local_key()) == 32


def test_decode_password_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="未知的密码编码方式"):
        crypto.decode_password("x", "rot13")


def test_decode_password_survives_broken_base64() -> None:
    """门户里存着坏掉的 base64 时返回空串，不能抛异常炸掉比对。"""
    assert crypto.decode_password("！！！", "base64") == ""
    assert crypto.decode_password("！！！", "md5_base64") == ""


def test_decode_password_passthrough_modes() -> None:
    """摘要不可逆，md5/sha256 原样返回，仅供日志比对。"""
    assert crypto.decode_password("deadbeef", "md5") == "deadbeef"
    assert crypto.decode_password("cafe", "sha256") == "cafe"


def test_v2_token_with_missing_parts(posix, no_dpapi) -> None:
    assert crypto.deobfuscate("v2.只有一段") == ""


def test_local_key_regenerates_when_file_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # noqa: ANN001
    """密钥文件被写坏（不是合法 base64）时重新生成，不让整条链路崩掉。"""
    key_file = tmp_path / "obf.key"
    key_file.write_text("这不是 base64", encoding="utf-8")
    monkeypatch.delenv("CAMPUSNET_OBF_KEY", raising=False)
    monkeypatch.setenv("CAMPUSNET_KEY_FILE", str(key_file))

    assert len(crypto._local_key()) == 32


def test_unb64_rejects_bad_encoding() -> None:
    with pytest.raises(crypto.CredentialExportError):
        crypto._unb64("！！！")


def test_export_blob_rejects_empty_passphrase() -> None:
    with pytest.raises(crypto.CredentialExportError, match="不能为空"):
        crypto.encrypt_blob(b"data", "")


def test_decrypt_blob_error_paths() -> None:
    with pytest.raises(crypto.CredentialExportError, match="结构不对"):
        crypto.decrypt_blob("not-a-dict", "pw")

    envelope = crypto.encrypt_blob(b"data", "pw", iterations=1000)
    with pytest.raises(crypto.CredentialExportError, match="版本或算法"):
        crypto.decrypt_blob({**envelope, "version": 99}, "pw")

    with pytest.raises(crypto.CredentialExportError, match="不能为空"):
        crypto.decrypt_blob(envelope, "")

    broken = {k: v for k, v in envelope.items() if k != "iterations"}
    with pytest.raises(crypto.CredentialExportError, match="缺少 KDF 参数"):
        crypto.decrypt_blob(broken, "pw")

    with pytest.raises(crypto.CredentialExportError, match="参数"):
        crypto.decrypt_blob({**envelope, "iterations": "abc"}, "pw")


def test_decrypt_blob_rejects_short_body() -> None:
    envelope = crypto.encrypt_blob(b"data", "pw", iterations=1000)
    short = base64.urlsafe_b64encode(b"\x00" * 8).decode("ascii")

    with pytest.raises(crypto.CredentialExportError, match="内容不完整"):
        crypto.decrypt_blob({**envelope, "body": short}, "pw")


def test_export_blob_roundtrip_with_fixed_params() -> None:
    """固定 salt/nonce 时输出稳定，便于核对封套格式；口令错则报错。"""
    salt = b"\x01" * crypto.EXPORT_SALT_BYTES
    nonce = b"\x02" * crypto.EXPORT_NONCE_BYTES
    envelope = crypto.encrypt_blob(
        "中文口令".encode("utf-8"), "pw", iterations=1000, salt=salt, nonce=nonce
    )

    assert envelope["kdf"] == crypto.EXPORT_KDF
    assert crypto.decrypt_blob(envelope, "pw") == "中文口令".encode("utf-8")

    with pytest.raises(crypto.CredentialExportError, match="口令不对"):
        crypto.decrypt_blob(envelope, "wrong")