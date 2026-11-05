import pytest
from aiogram.methods import SendMessage
from sqlalchemy import select

from chat import mock_plans
from chat.bot import create_dispatcher
from chat.bot.delivery import EventDelivery
from chat.notifier import Notifier
from shared.config import get_settings
from shared.models import AgentRun, ChatMessage, TripPlan, TripPlanVersion

from .fake_telegram import make_bot, message_update


@pytest.fixture
def demo_mode(monkeypatch, db):
    monkeypatch.setenv("DEV_AGENT", "true")
    get_settings.cache_clear()


async def plans(db):
    async with db.sessionmaker() as s:
        return list(await s.scalars(select(TripPlan)))


async def test_demo_gives_the_chat_a_ready_plan_and_announces_it_with_the_map_button(db, demo_mode):
    bot, session = make_bot()
    await create_dispatcher().feed_update(bot, message_update(1, "/demo"))
    (plan,) = await plans(db)
    assert plan.status == "ready" and plan.view_json["trip_id"] == "demo_moscow_spb"
    await EventDelivery(bot, db, get_settings(), Notifier()).deliver_pending()
    msg = session.of(SendMessage)[-1]
    assert "Москва и Санкт-Петербург" in msg.text
    assert msg.reply_markup.inline_keyboard[0][0].web_app.url.endswith(f"plan_id={plan.id}")
    async with db.sessionmaker() as s:
        assert (await s.scalars(select(AgentRun))).all() == []  # /demo bypasses the agents entirely
        assistant = (await s.scalars(select(ChatMessage).where(ChatMessage.role == "assistant"))).one()
        assert assistant.plan_id == plan.id


async def test_demo_japan_and_repeat_bumps_the_revision(db, demo_mode):
    bot, _ = make_bot()
    dp = create_dispatcher()
    await dp.feed_update(bot, message_update(1, "/demo japan"))
    await dp.feed_update(bot, message_update(2, "/demo"))
    (plan,) = await plans(db)  # one plan per chat, edited in place
    assert plan.revision == 2 and plan.view_json["trip_id"] == "demo_moscow_spb"
    async with db.sessionmaker() as s:
        versions = list(await s.scalars(select(TripPlanVersion).order_by(TripPlanVersion.revision)))
    assert [v.view_json["trip_id"] for v in versions] == ["trip_02", "demo_moscow_spb"]


async def test_demo_with_an_unknown_argument_explains_usage(db, demo_mode):
    bot, session = make_bot()
    await create_dispatcher().feed_update(bot, message_update(1, "/demo mars"))
    assert await plans(db) == [] and "/demo japan" in session.of(SendMessage)[0].text


async def test_demo_is_off_without_dev_agent(db, monkeypatch):
    monkeypatch.delenv("DEV_AGENT", raising=False)
    get_settings.cache_clear()
    bot, session = make_bot()
    await create_dispatcher().feed_update(bot, message_update(1, "/demo"))
    assert await plans(db) == [] and "демо-планов в этой версии нет" in session.of(SendMessage)[0].text


def test_scenario_arguments():
    assert mock_plans.parse_scenario(None) == mock_plans.parse_scenario("") == "moscow_spb"
    assert mock_plans.parse_scenario(" Питер ") == "moscow_spb"
    assert mock_plans.parse_scenario("japan") == mock_plans.parse_scenario("токио") == "japan"
    assert mock_plans.parse_scenario("mars") is None
