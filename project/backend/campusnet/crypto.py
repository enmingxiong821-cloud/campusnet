"""密码编码与本地混淆存储。

混淆仅用于避免明文直接躺在磁盘上，不是加密。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

_PASSWORD_MODES = ("plain", "base64", "md5", "md5_base64", "sha256")


def encode_password(password: str, mode: str) -> str:
    if mode not in _PASSWORD_MODES:
        raise ValueError(f"未知的密码编码方式: {mode}")
    if mode == "plain":
        return password
    if mode == "base64":
        return base64.b64encode(password.encode("utf-8")).decode("ascii")
    if mode in ("md5", "md5_base64"):
        digest = hashlib.md5(password.encode("utf-8")).hexdigest()
        if mode == "md5":
            return digest
        return base64.b64encode(digest.encode("ascii")).decode("ascii")
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def decode_password(value: str, mode: str) -> str:
    """把门户表单里的密码字段还原成明文，用于比对是否需要重连。"""
    if mode not in _PASSWORD_MODES:
        raise ValueError(f"未知的密码编码方式: {mode}")
    if mode == "plain":
        return value
    if mode == "base64":
        try:
            return base64.b64decode(value.encode("ascii")).decode("utf-8")
        except Exception:
            return ""
    if mode == "md5_base64":
        try:
            return base64.b64decode(value.encode("ascii")).decode("ascii")
        except Exception:
            return ""
    if mode == "md5":
        return value  # 摘要不可逆，仅供日志比对
    return value


def obfuscate(password: str) -> str:
    """本地落盘混淆：随机密钥 + HMAC 派生流密码。

    能挡住明文肉眼检索，挡不住本机 root；够用即可。
    """
    key = _local_key()
    raw = password.encode("utf-8")
    stream = _keystream(key, len(raw))
    mixed = bytes(b ^ k for b, k in zip(raw, stream))
    blob = base64.urlsafe_b64encode(mixed).decode("ascii")
    return f"v1.{base64.urlsafe_b64encode(key).decode('ascii')}.{blob}"


def deobfuscate(token: str) -> str:
    try:
        version, key_b64, blob = token.split(".", 2)
        if version != "v1":
            return ""
        key = base64.urlsafe_b64decode(key_b64.encode("ascii"))
        raw = base64.urlsafe_b64decode(blob.encode("ascii"))
    except Exception:
        return ""
    stream = _keystream(key, len(raw))
    return bytes(b ^ k for b, k in zip(raw, stream)).decode("utf-8", errors="replace")


def _local_key() -> bytes:
    """密钥优先来自环境变量；否则在用户目录放一个 .key 文件。"""
    env = os.environ.get("CAMPUSNET_OBF_KEY")
    if env:
        return hashlib.sha256(env.encode("utf-8")).digest()
    key_file = Path(os.environ.get("CAMPUSNET_KEY_FILE", _key_file_path())).expanduser()
    try:
        if key_file.exists():
            return base64.urlsafe_b64decode(key_file.read_text(encoding="utf-8").strip().encode("ascii"))
    except Exception:
        pass
    key = secrets.token_bytes(32)
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(base64.urlsafe_b64encode(key).decode("ascii"), encoding="utf-8")
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key


def _key_file_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".campusnet_obf.key")


def _keystream(key: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hmac.new(key, counter.to_bytes(4, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])