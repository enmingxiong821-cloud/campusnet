"""密码编码与本地凭据封装。

Windows 优先使用当前用户 DPAPI；其他平台和 DPAPI 不可用时使用带完整性
校验的本机密钥封装。旧版 v1 token 继续可读，避免升级时丢失已有账号。
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
    """封装密码，优先使用 Windows 当前用户 DPAPI。"""
    if os.name == "nt":
        token = _dpapi_protect(password.encode("utf-8"))
        if token:
            return f"v3.dpapi.{base64.urlsafe_b64encode(token).decode('ascii')}"

    key = _local_key()
    raw = password.encode("utf-8")
    nonce = secrets.token_bytes(16)
    stream = _keystream(key + nonce, len(raw))
    mixed = bytes(b ^ k for b, k in zip(raw, stream))
    payload = nonce + mixed
    mac = hmac.new(key, payload, hashlib.sha256).digest()
    return (
        "v2."
        f"{base64.urlsafe_b64encode(payload).decode('ascii')}."
        f"{base64.urlsafe_b64encode(mac).decode('ascii')}"
    )


def deobfuscate(token: str) -> str:
    if token.startswith("v3.dpapi."):
        try:
            raw = base64.urlsafe_b64decode(token.split(".", 2)[2].encode("ascii"))
        except Exception:
            return ""
        plain = _dpapi_unprotect(raw) if os.name == "nt" else None
        return plain.decode("utf-8", errors="replace") if plain is not None else ""

    if token.startswith("v2."):
        try:
            _, payload_b64, mac_b64 = token.split(".", 2)
            payload = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
            expected = base64.urlsafe_b64decode(mac_b64.encode("ascii"))
            if len(payload) < 16:
                return ""
            key = _local_key()
            actual = hmac.new(key, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(actual, expected):
                return ""
            nonce, mixed = payload[:16], payload[16:]
            raw = bytes(b ^ k for b, k in zip(mixed, _keystream(key + nonce, len(mixed))))
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return ""

    # v1 compatibility: the old token carried its stream key in the value.
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


def _dpapi_protect(raw: bytes) -> bytes | None:
    if os.name != "nt":
        return None
    try:
        import ctypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        source = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        inp = Blob(len(raw), source)
        out = Blob()
        flags = 0x01  # CRYPTPROTECT_UI_FORBIDDEN
        if not crypt32.CryptProtectData(ctypes.byref(inp), None, None, None, None, flags, ctypes.byref(out)):
            return None
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(out.pbData)
    except Exception:
        return None


def _dpapi_unprotect(raw: bytes) -> bytes | None:
    if os.name != "nt":
        return None
    try:
        import ctypes

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        source = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        inp = Blob(len(raw), source)
        out = Blob()
        flags = 0x01
        if not crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, flags, ctypes.byref(out)):
            return None
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(out.pbData)
    except Exception:
        return None


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
