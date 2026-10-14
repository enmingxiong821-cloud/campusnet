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
        # 以 0600 建好再写：write_text 是按 umask 落盘的（POSIX 通常 0644），
        # 「写完再 chmod」中间那段窗口里，同机其他用户就能读到这把钥匙——
        # 读到它等于解开库里所有账号密码。
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, base64.urlsafe_b64encode(key))
            # 老版本留下的 0644 文件不会被 O_CREAT 的 mode 改过来，补一次；
            # 就算这里失败，文件已是本次以 0600 新建的，不是安全缺口。
            os.chmod(key_file, 0o600)
        finally:
            os.close(fd)
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


class CredentialExportError(Exception):
    """可移植凭据封套的加解密失败：口令不对、文件被改动或格式不受支持。"""


EXPORT_KDF = "pbkdf2-sha256"
EXPORT_VERSION = 1
EXPORT_ITERATIONS = 200_000
EXPORT_SALT_BYTES = 16
EXPORT_NONCE_BYTES = 16


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text.encode("ascii"))
    except Exception as exc:  # noqa: BLE001 - 编码坏掉一律当成文件损坏
        raise CredentialExportError("导出文件里的编码非法") from exc


def encrypt_blob(
    plaintext: bytes,
    passphrase: str,
    *,
    iterations: int = EXPORT_ITERATIONS,
    salt: bytes | None = None,
    nonce: bytes | None = None,
) -> dict[str, object]:
    """用导出口令加密一段数据，返回可直接 json.dumps 的封套。

    与本机封装的区别：本机封装绑机器/用户（Windows 走 DPAPI，其它平台走
    ~/.campusnet_obf.key），所以备份数据库无法跨机恢复；这份封套只绑口令，
    换机、重装系统后仍能解——代价是必须选一个足够强的口令。
    """
    if not passphrase:
        raise CredentialExportError("导出口令不能为空")
    salt = salt or secrets.token_bytes(EXPORT_SALT_BYTES)
    nonce = nonce or secrets.token_bytes(EXPORT_NONCE_BYTES)
    key = hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), salt, int(iterations)
    )
    mixed = bytes(
        byte ^ item
        for byte, item in zip(plaintext, _keystream(key + nonce, len(plaintext)))
    )
    body = nonce + mixed
    mac = hmac.new(key, salt + body, hashlib.sha256).digest()
    return {
        "version": EXPORT_VERSION,
        "kdf": EXPORT_KDF,
        "iterations": int(iterations),
        "salt": _b64(salt),
        "body": _b64(body),
        "mac": _b64(mac),
    }


def decrypt_blob(envelope: object, passphrase: str) -> bytes:
    if not isinstance(envelope, dict):
        raise CredentialExportError("导出文件结构不对")
    if envelope.get("version") != EXPORT_VERSION or envelope.get("kdf") != EXPORT_KDF:
        raise CredentialExportError("导出文件版本或算法不受支持")
    if not passphrase:
        raise CredentialExportError("导入口令不能为空")
    try:
        iterations = int(envelope["iterations"])  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError) as exc:
        raise CredentialExportError("导出文件缺少 KDF 参数") from exc
    salt = _unb64(str(envelope.get("salt", "")))
    body = _unb64(str(envelope.get("body", "")))
    expected = _unb64(str(envelope.get("mac", "")))
    if len(body) <= EXPORT_NONCE_BYTES:
        raise CredentialExportError("导出文件内容不完整")
    key = hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), salt, iterations
    )
    actual = hmac.new(key, salt + body, hashlib.sha256).digest()
    if not hmac.compare_digest(actual, expected):
        # 口令错与文件被改在密码学上不可区分，如实说明两种可能。
        raise CredentialExportError("口令不对，或导出文件已被改动")
    nonce, mixed = body[:EXPORT_NONCE_BYTES], body[EXPORT_NONCE_BYTES:]
    return bytes(
        byte ^ item for byte, item in zip(mixed, _keystream(key + nonce, len(mixed)))
    )
