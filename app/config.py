"""Sozlamalar muhit o'zgaruvchilaridan (.env) o'qiladi."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _ints(value: str) -> list[int]:
    return [int(x) for x in value.replace(" ", "").split(",") if x]


@dataclass
class Settings:
    api_id: int = int(os.environ.get("TG_API_ID", "0"))
    api_hash: str = os.environ.get("TG_API_HASH", "")
    session_string: str = os.environ.get("TG_SESSION_STRING", "")
    bot_token: str = os.environ.get("TG_BOT_TOKEN", "")
    admin_ids: list[int] = field(default_factory=lambda: _ints(os.environ.get("ADMIN_IDS", "")))
    ai_model: str = os.environ.get("AI_MODEL", "claude-opus-5-5")
    database_url: str = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///data/agent.db")
    seed_file: str = os.environ.get("SEED_FILE", "seed.json")
    # "Aqlli" rejimda shu ishonchdan past qarorlar adminga yuboriladi
    confidence_threshold: float = float(os.environ.get("AI_CONFIDENCE", "0.8"))
    # Veb admin panel
    admin_user: str = os.environ.get("ADMIN_USER", "admin")
    admin_password: str = os.environ.get("ADMIN_PASSWORD", "")
    secret_key: str = os.environ.get("SECRET_KEY", "") or os.urandom(32).hex()
    cookie_secure: bool = os.environ.get("COOKIE_SECURE", "0") == "1"  # HTTPS ortida 1 qiling
    web_host: str = os.environ.get("WEB_HOST", "127.0.0.1")
    web_port: int = int(os.environ.get("WEB_PORT", "8080"))
    stats_interval_sec: int = int(os.environ.get("STATS_INTERVAL_SEC", "3600"))


settings = Settings()
