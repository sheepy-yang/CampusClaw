"""配置：环境变量优先，根目录 .env 补充；关键密钥缺失即失败。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
TMP_DIR = UPLOAD_DIR / ".tmp"
DB_PATH = DATA_DIR / "campusclaw.db"
WEB_DIR = BASE_DIR / "web"


def _load_dotenv() -> None:
    """极简 .env 解析：KEY=VALUE，支持 # 注释与可选 export 前缀。

    只填充环境中尚不存在的键（真实环境变量优先）。
    """
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Config:
    session_secret: str
    host: str
    port: int
    session_ttl_seconds: int
    max_upload_bytes: int
    login_max_failures: int
    login_window_seconds: int

    @staticmethod
    def load(*, require_secret: bool = True) -> "Config":
        _load_dotenv()
        secret = os.environ.get("SESSION_SECRET", "").strip()
        if require_secret and (not secret or secret == "change-me-to-a-long-random-string"):
            raise SystemExit(
                "配置错误：未设置 SESSION_SECRET。\n"
                "请复制 .env.example 为 .env 并填入高熵值，或设置环境变量后重试。\n"
                '生成方法：python -c "import secrets;print(secrets.token_urlsafe(48))"'
            )
        return Config(
            session_secret=secret,
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "5000")),
            session_ttl_seconds=int(os.environ.get("SESSION_TTL_SECONDS", "28800")),
            max_upload_bytes=int(os.environ.get("MAX_UPLOAD_BYTES", str(2 * 1024 * 1024))),
            login_max_failures=int(os.environ.get("LOGIN_MAX_FAILURES", "5")),
            login_window_seconds=int(os.environ.get("LOGIN_WINDOW_SECONDS", "900")),
        )
