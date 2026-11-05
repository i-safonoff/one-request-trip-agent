"""Application-wide settings (environment / .env). Module-specific settings live in their modules."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    # PostgreSQL in production (postgresql+asyncpg://...); SQLite keeps local runs and tests Docker-free.
    database_url: str = "sqlite+aiosqlite:///./data/dev.db"
    # Create tables on startup. Handy for dev/tests; production uses Alembic migrations.
    auto_create_tables: bool = True

    # Public HTTPS origin of this backend. Telegram only opens Mini Apps from https URLs.
    public_base_url: str = "http://localhost:8080"
    # A file the tunnel container writes its current https address into (quick tunnels change address on every restart).
    # When it exists, it overrides PUBLIC_BASE_URL and is re-read while the app runs.
    public_url_file: str | None = None
    # Where planning happens. "miniapp": the bot only opens the Mini App (one place for chat, plan and checkout, so the
    # surfaces cannot disagree); "chat": the bot also plans in the Telegram chat; "auto": miniapp when the app has an https address.
    telegram_chat_mode: str = "auto"

    telegram_bot_token: str | None = None
    # With a webhook URL the bot receives updates on POST /telegram/webhook; without one it long-polls (dev).
    telegram_webhook_url: str | None = None
    telegram_webhook_secret: str | None = None

    # Signs API session tokens. Falls back to a key derived from the bot token.
    session_secret: str | None = None
    session_ttl_seconds: int = 24 * 3600
    # Telegram initData older than this is rejected (replay protection).
    init_data_max_age_seconds: int = 24 * 3600

    # Trip history: feedback is only accepted after the trip ended, unless this dev switch is on (demo trips are
    # in the future). Half-life of how fast an old observation loses weight, and the confidence needed to be proposed.
    history_allow_early_feedback: bool = False
    history_half_life_days: int = 365
    history_min_confidence: float = 0.4

    # Run the stub agent worker (chat/dev_agent.py) until the real agents module exists.
    dev_agent: bool = False


def refresh_public_url(settings: "Settings") -> bool:
    """Take the tunnel's current address from `public_url_file`; True when the address changed."""
    if not settings.public_url_file:
        return False
    try:
        url = open(settings.public_url_file, encoding="utf-8").read().strip()
    except OSError:
        return False
    if url.lower().startswith("https://") and url != settings.public_base_url:
        settings.public_base_url = url
        return True
    return False


@lru_cache
def get_settings() -> Settings:
    return Settings()
