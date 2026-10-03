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