"""安全原语：口令慢哈希、JWT 访问令牌。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

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
    """返回 (随机串, SHA-256 哈希)。用于临时文件名等，不作为登录凭证。"""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    return token, token_hash


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


JWT_ISSUER = "campusclaw"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    pad = "=" * ((4 - len(segment) % 4) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def issue_access_token(
    secret: str,
    *,
    user_id: int,
    username: str,
    role: str,
    class_id: int,
    ttl_seconds: int,
) -> tuple[str, int]:
    """签发 HS256 JWT。返回 (token, exp unix 秒)。

    声明仅供客户端展示；授权仍以服务端按 token 哈希查出的用户行为准。
    """
    now = int(time.time())
    exp = now + ttl_seconds
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "user_id": user_id,
        "username": username,
        "role": role,
        "class_id": class_id,
        "iss": JWT_ISSUER,
        "sub": username,
        "iat": now,
        "exp": exp,
    }
    signing_input = ".".join((
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")),
    ))
    sig = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(sig)}", exp


def decode_access_token(token: str, secret: str) -> dict | None:
    """校验 HS256 签名、签发方与 exp。失败返回 None，不抛异常。"""
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(_b64url_decode(header_b64))
        if not isinstance(header, dict) or header.get("alg") != "HS256":
            return None
        expected = hmac.new(
            secret.encode("utf-8"),
            f"{header_b64}.{payload_b64}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64url_decode(sig_b64), expected):
            return None
        payload = json.loads(_b64url_decode(payload_b64))
        if not isinstance(payload, dict):
            return None
        if payload.get("iss") != JWT_ISSUER:
            return None
        exp = payload.get("exp")
        if not isinstance(exp, int) or exp <= int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
