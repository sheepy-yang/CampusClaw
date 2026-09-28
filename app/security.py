"""安全原语：口令慢哈希、会话 token。"""
from __future__ import annotations

import hashlib
import hmac
import secrets

PBKDF2_ALGO = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return f"{PBKDF2_ALGO}${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iter_s, salt_hex, hash_hex = stored.split("$")
        if algo != PBKDF2_ALGO:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iter_s),
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def dummy_verify(password: str) -> bool:
    """账号不存在时走一次等开销哈希，抹平登录耗时差异。"""
    # 固定盐与迭代次数，仅用于消耗与真实验密近似的时间
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        b"\x00" * SALT_BYTES,
        PBKDF2_ITERATIONS,
    )
    return hmac.compare_digest(digest, digest) and False


def new_session_token() -> tuple[str, str]:
    """返回 (原始 token 写入 Cookie, SHA-256 哈希 入库)。"""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    return token, token_hash


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()
