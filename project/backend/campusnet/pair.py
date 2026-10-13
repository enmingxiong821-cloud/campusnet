"""扫码配对：生成一次性配对码，让手机不必手抄 43 位控制台令牌。

安全模型：
- 配对码只在生成时返回一次明文，库里只存 SHA-256 哈希；
- 默认 5 分钟过期、单次使用、连续失败达上限即锁定；
- 成功与失败都记事件，便于事后发现异常配对。

字符表用 Crockford base32（去掉 I / L / O / U），避免抄错、大小写不敏感。
6 位约 30 比特熵，配合 5 分钟 TTL 与单次使用，暴力猜解不现实。
"""

from __future__ import annotations

import hashlib
import secrets

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 6
PAIR_TTL_SECONDS = 300  # 5 分钟
MAX_FAILURES = 3


def generate_code(length: int = CODE_LENGTH) -> str:
    """生成一个配对码（大写字母 + 数字，无易混淆字符）。"""
    return "".join(secrets.choice(CROCKFORD) for _ in range(length))


def code_hash(code: str) -> str:
    """配对码的 SHA-256 十六进制哈希；库里只存它。"""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()
