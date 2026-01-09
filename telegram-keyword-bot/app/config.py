from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    rule_cooldown_seconds: int = 8
    rule_cache_ttl_seconds: int = 60

def get_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not bot_token:
        raise RuntimeError("Missing BOT_TOKEN in environment (.env).")
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL in environment (.env).")
    cooldown = int(os.getenv("RULE_COOLDOWN_SECONDS", "8"))
    ttl = int(os.getenv("RULE_CACHE_TTL_SECONDS", "60"))
    return Settings(
        bot_token=bot_token,
        database_url=database_url,
        rule_cooldown_seconds=cooldown,
        rule_cache_ttl_seconds=ttl,
    )
