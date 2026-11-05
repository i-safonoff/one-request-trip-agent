"""The whole chat <-> agents contract end to end, with the dev agent standing in for the agents module."""
from aiogram.methods import SendMessage
from sqlalchemy import select

from chat.bot import create_dispatcher
from chat.bot.delivery import EventDelivery
from chat.dev_agent import DevAgent
from chat.notifier import Notifier
from shared.config import get_settings
from shared.models import AgentRun, ChatMessage, Clarification, TripPlan, TripPlanVersion

from .fake_telegram import callback_update, make_bot, message_update


async def test_request_clarification_answer_plan(db):
    bot, session = make_bot()
    dp, agent, delivery = create_dispatcher(), DevAgent(db, step_delay=0), EventDelivery(bot, db, get_settings(), Notifier())

    # 1. a request without details: the agent asks for them
    await dp.feed_update(bot, message_update(1, "Хочу в Японию"))
    assert await agent.step() is True
    await delivery.deliver_pending()
    question = session.of(SendMessage)[-1]
    assert "Сколько человек" in question.text
    buttons = [b for row in question.reply_markup.inline_keyboard for b in row]
    assert len(buttons) == 3

    async with db.sessionmaker() as s:
        run = (await s.scalars(select(AgentRun))).one()
        assert run.status == "waiting_clarification"  # the question does not hold the queue
        q = (await s.scalars(select(Clarification))).one()

    # 2. the user taps a quick reply: it becomes a ProcessClarificationAnswer run
    await dp.feed_update(bot, callback_update(2, buttons[0].callback_data))
    assert await agent.step() is True
    await delivery.deliver_pending()

    # 3. the plan is stored, versioned, and announced with the map button
    async with db.sessionmaker() as s:
        plan = (await s.scalars(select(TripPlan))).one()
        assert plan.status == "ready" and plan.revision == 1 and plan.view_json["title"]
        assert len((await s.scalars(select(TripPlanVersion))).all()) == 1
        runs = list(await s.scalars(select(AgentRun)))
        assert sorted(r.status for r in runs) == ["done", "done"]
        assert (await s.get(Clarification, q.id)).status == "answered"
        assistant = (await s.scalars(select(ChatMessage).where(ChatMessage.role == "assistant"))).one()
        assert assistant.plan_id == plan.id and assistant.plan_revision == 1
    final = session.of(SendMessage)[-1]
    assert "Собрал поездку" in final.text
    assert final.reply_markup.inline_keyboard[0][0].web_app.url.endswith(f"plan_id={plan.id}")

    # 4. a follow-up edits the same plan: new revision, old one kept
    await dp.feed_update(bot, message_update(3, "Сделай дешевле, 2 взрослых"))
    await agent.step()
    async with db.sessionmaker() as s:
        plan = (await s.scalars(select(TripPlan))).one()
        assert plan.revision == 2
        assert sorted(v.revision for v in await s.scalars(select(TripPlanVersion))) == [1, 2]


async def test_runs_of_one_chat_are_processed_one_at_a_time_in_order(db):
    bot, _ = make_bot()
    dp, agent = create_dispatcher(), DevAgent(db, step_delay=0)
    for i in (1, 2):
        await dp.feed_update(bot, message_update(i, f"запрос {i}, 2 человека"))
    first = await agent.step()
    async with db.sessionmaker() as s:
        statuses = [r.status for r in await s.scalars(select(AgentRun).order_by(AgentRun.created_at))]
    assert first and statuses == ["done", "queued"]  # the second waited for the first
    assert await agent.step() is True and await agent.step() is False  # then nothing is left
