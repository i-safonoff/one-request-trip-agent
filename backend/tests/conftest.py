import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.config import Settings as AgentSettings  # noqa: E402
from agents.integrations.hotels import HotelQuery, HotelResult, normalize_rates, normalize_reviews  # noqa: E402
from agents.integrations.llm import FakeLLM  # noqa: E402
from agents.integrations.transport import MockTransport  # noqa: E402
from agents.runtime.destinations import DestinationAgent  # noqa: E402
from agents.runtime.memory_store import MemoryPlanStore  # noqa: E402
from agents.runtime.orchestrator import Orchestrator  # noqa: E402
from agents.runtime.search import Searcher  # noqa: E402
from shared.config import Settings, get_settings  # noqa: E402
from shared.database import Database, set_database  # noqa: E402

TEST_BOT_TOKEN = "123456:TEST-TOKEN"
FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- database (chat / history / agents worker)
@pytest.fixture
async def db(monkeypatch):
    """Fresh in-memory database and test settings for every test."""
    # tests must not depend on the developer's real .env (bot token, DEV_AGENT, ...)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TEST_BOT_TOKEN)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("TELEGRAM_CHAT_MODE", "chat")  # the Telegram chat tests plan in the chat; Mini App mode has its own tests
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    get_settings.cache_clear()
    # TEST_DATABASE_URL=postgresql+asyncpg://...  runs the same tests on PostgreSQL (SKIP LOCKED, partial indexes, JSONB)
    pg = os.environ.get("TEST_DATABASE_URL")
    database = Database(pg or "sqlite+aiosqlite://")
    if pg:
        from shared.models import Base
        async with database.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    else:
        await database.create_tables()
    set_database(database)
    yield database
    set_database(None)
    await database.dispose()
    get_settings.cache_clear()


# ---------------------------------------------------------------- agents: scripted LLM + recorded Tutu data
class FixtureHotels:
    """Serves the real (recorded) Tutu hotel details for any Moscow query, honouring the filters."""

    def __init__(self, reviews_fixture: str = "tutu_reviews_cosmos_smart.json"):
        self.queries: list[HotelQuery] = []
        self.reviews_fixture = reviews_fixture
        self.review_calls: list[str] = []

    async def reviews(self, hotel_id: str, q: HotelQuery):
        self.review_calls.append(hotel_id)
        return normalize_reviews(json.loads((FIX / self.reviews_fixture).read_text("utf-8")))

    async def search(self, q: HotelQuery) -> HotelResult:
        self.queries.append(q)
        d = json.loads((FIX / "tutu_offer_details_cosmos_smart.json").read_text("utf-8"))
        offers = normalize_rates(d, NOW, max_rooms=3, need_breakfast=q.breakfast, need_cancel=q.free_cancellation)
        return HotelResult(offers, "partial", resolved_name=q.city)


@pytest.fixture
def settings():
    return AgentSettings(hotels_source="tutu", transport_source="mock", nominatim_enabled=False)


@pytest.fixture
def make_orch(settings):
    def make(script: dict, hotels=None):
        llm = FakeLLM(script)
        store = MemoryPlanStore()
        hotels = hotels or FixtureHotels()
        searcher = Searcher(hotels, MockTransport(), settings, geocoder=None)
        orch = Orchestrator(llm, searcher, DestinationAgent(llm, hotels, settings), store, settings, now=lambda: NOW)
        return orch, store, llm, hotels
    return make
