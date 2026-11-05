"""Runtime settings, read from the environment (see .env.example)."""
import os
from dataclasses import dataclass, field
from functools import lru_cache


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:

    # the user's own calendar day ("завтра" at 00:49 Moscow time is not the UTC date)
    user_tz: str = field(default_factory=lambda: os.environ.get("USER_TZ", "Europe/Moscow"))

    # LLM (GigaChat)
    gigachat_auth_key: str = field(default_factory=lambda: os.environ.get("GIGACHAT_AUTH_KEY", ""))
    gigachat_scope: str = field(default_factory=lambda: os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"))
    gigachat_model: str = field(default_factory=lambda: os.environ.get("GIGACHAT_MODEL", "GigaChat-2-Max"))
    gigachat_verify_ssl: bool = field(default_factory=lambda: _bool("GIGACHAT_VERIFY_SSL_CERTS", False))
    gigachat_ca_bundle: str = field(default_factory=lambda: os.environ.get("GIGACHAT_CA_BUNDLE_FILE", ""))
    gigachat_base_url: str = field(default_factory=lambda: os.environ.get("GIGACHAT_BASE_URL", ""))  # empty = SDK default
    gigachat_max_concurrency: int = field(default_factory=lambda: int(os.environ.get("GIGACHAT_MAX_CONCURRENCY", "1")))
    # json_schema response_format makes GigaChat flatten nested schemas (add=2 instead of a list); prompt-only JSON is more reliable
    gigachat_json_schema: bool = field(default_factory=lambda: _bool("GIGACHAT_JSON_SCHEMA", False))
    gigachat_timeout: float = field(default_factory=lambda: float(os.environ.get("GIGACHAT_TIMEOUT", "60")))

    # Data sources
    tutu_mcp_url: str = field(default_factory=lambda: os.environ.get("TUTU_MCP_URL", "https://mcp.tutu.ru/mcp"))
    tutu_timeout: float = field(default_factory=lambda: float(os.environ.get("TUTU_TIMEOUT", "30")))
    hotels_source: str = field(default_factory=lambda: os.environ.get("HOTELS_SOURCE", "auto"))  # auto | tutu | mock
    transport_source: str = field(default_factory=lambda: os.environ.get("TRANSPORT_SOURCE", "auto"))  # auto | tutu | mock
    warm_sea_min_c: float = field(default_factory=lambda: float(os.environ.get("WARM_SEA_MIN_C", "22")))
    nominatim_enabled: bool = field(default_factory=lambda: _bool("NOMINATIM_ENABLED", True))

    # Worker
    worker_poll_seconds: float = field(default_factory=lambda: float(os.environ.get("WORKER_POLL_SECONDS", "1.0")))
    run_stale_seconds: int = field(default_factory=lambda: int(os.environ.get("RUN_STALE_SECONDS", "60")))
    run_deadline_seconds: float = field(default_factory=lambda: float(os.environ.get("RUN_DEADLINE_SECONDS", "150")))


@lru_cache
def get_settings() -> Settings:
    return Settings()
