"""Mini App mode: with an https address the bot only opens the app; planning, plan and checkout live in one place."""
import pytest
from aiogram.methods import SendMessage
from sqlalchemy import select

from chat.bot import create_dispatcher
from shared.config import get_settings
from shared.models import AgentRun

from .fake_telegram import make_bot, message_update


@pytest.fixture
def https_app(monkeypatch, db):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example.test")
    monkeypatch.setenv("TELEGRAM_CHAT_MODE", "auto")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_free_text_opens_the_app_instead_of_planning_in_the_chat(db, https_app):
    bot, session = make_bot()
    await create_dispatcher().feed_update(bot, message_update(1, "Хочу в Питер на выходные"))
    msg = session.of(SendMessage)[-1]
    assert "в приложении" in msg.text
    assert msg.reply_markup.inline_keyboard[0][0].web_app.url == "https://app.example.test/"
    async with db.sessionmaker() as s:
        assert (await s.scalars(select(AgentRun))).all() == []  # nothing was queued: the chat did not plan


@pytest.mark.parametrize("cmd", ["/start", "/help", "/plan", "/status"])
async def test_commands_point_to_the_app(db, https_app, cmd):
    bot, session = make_bot()
    await create_dispatcher().feed_update(bot, message_update(1, cmd))
    assert session.of(SendMessage)[-1].reply_markup.inline_keyboard[0][0].web_app.url == "https://app.example.test/"


async def test_without_https_the_chat_still_plans(db, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("TELEGRAM_CHAT_MODE", "auto")
    get_settings.cache_clear()
    bot, session = make_bot()
    await create_dispatcher().feed_update(bot, message_update(1, "Хочу в Питер на выходные"))
    async with db.sessionmaker() as s:
        assert len((await s.scalars(select(AgentRun))).all()) == 1


async def test_mode_can_be_forced_to_chat(db, https_app, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_MODE", "chat")
    get_settings.cache_clear()
    bot, _ = make_bot()
    await create_dispatcher().feed_update(bot, message_update(1, "Хочу в Питер на выходные"))
    async with db.sessionmaker() as s:
        assert len((await s.scalars(select(AgentRun))).all()) == 1
