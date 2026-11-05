import uuid

import httpx
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, SendMessage
from sqlalchemy import select

from chat.bot import create_dispatcher
from chat.bot.delivery import EventDelivery
from chat.bot.render import chunks
from chat.notifier import Notifier
from chat.service import ChatService
from main import app
from shared.commands import agent_command_adapter
from shared.config import get_settings
from shared.events import (AgentProgress, AssistantMessage, ClarificationRequired, EventType, RequestFailed, emit_event)
from shared.models import AgentRun, Chat, ChatMessage, Clarification, TripPlan, User

from .fake_telegram import callback_update, make_bot, message_update


async def feed(dp, bot, update):
    await dp.feed_update(bot, update)


async def runs(db):
    async with db.sessionmaker() as s:
        return list(await s.scalars(select(AgentRun).order_by(AgentRun.created_at)))


# ---- handlers ----

async def test_start_registers_the_user_and_greets(db):
    bot, session = make_bot()
    await feed(create_dispatcher(), bot, message_update(1, "/start"))
    sent = session.of(SendMessage)
    assert len(sent) == 1 and "спланировать поездку" in sent[0].text
    async with db.sessionmaker() as s:
        assert (await s.scalar(select(User).where(User.telegram_id == 42))).username == "ann"
        assert (await s.scalars(select(Chat))).one().telegram_chat_id == 42


async def test_free_text_is_forwarded_to_agents_without_an_immediate_reply(db):
    bot, session = make_bot()
    await feed(create_dispatcher(), bot, message_update(2, "Москва, 6-9 ноября, двое взрослых"))
    assert session.of(SendMessage) == []  # the reply is delivered from chat_events
    (run,) = await runs(db)
    cmd = agent_command_adapter.validate_python(run.command)
    assert cmd.type == "ProcessUserMessage"
    async with db.sessionmaker() as s:
        assert (await s.scalars(select(ChatMessage))).one().text == "Москва, 6-9 ноября, двое взрослых"


async def test_telegram_redelivery_of_an_update_does_not_duplicate_the_request(db):
    bot, _ = make_bot()
    dp = create_dispatcher()
    await feed(dp, bot, message_update(3, "Токио", message_id=55))
    await feed(dp, bot, message_update(4, "Токио", message_id=55))  # same message_id, new update_id
    assert len(await runs(db)) == 1


async def test_commands_do_not_reach_the_agents(db):
    bot, session = make_bot()
    dp = create_dispatcher()
    for i, cmd in enumerate(["/help", "/status", "/plan", "/nope"], start=10):
        await feed(dp, bot, message_update(i, cmd))
    assert await runs(db) == []
    texts = [m.text for m in session.of(SendMessage)]
    assert "Команды:" in texts[0] and "Сейчас ничего не обрабатывается" in texts[1]
    assert "Плана пока нет" in texts[2] and "Не знаю такой команды" in texts[3]


async def test_new_starts_a_fresh_chat(db):
    bot, session = make_bot()
    dp = create_dispatcher()
    await feed(dp, bot, message_update(20, "/start"))
    await feed(dp, bot, message_update(21, "/new"))
    async with db.sessionmaker() as s:
        chats = list(await s.scalars(select(Chat).order_by(Chat.created_at)))
    assert [c.status for c in chats] == ["archived", "active"]
    assert "новую поездку" in session.of(SendMessage)[-1].text


async def test_plan_command_shows_the_map_button(db):
    bot, session = make_bot()
    dp = create_dispatcher()
    await feed(dp, bot, message_update(30, "/start"))
    async with db.sessionmaker() as s:
        chat = (await s.scalars(select(Chat))).one()
        plan = TripPlan(chat_id=chat.id, revision=3, view_json={"title": "Токио <и> Осака"})
        s.add(plan)
        await s.commit()
        plan_id = plan.id
    await feed(dp, bot, message_update(31, "/plan"))
    msg = session.of(SendMessage)[-1]
    assert "Токио &lt;и&gt; Осака" in msg.text and "Версия плана: 3" in msg.text
    button = msg.reply_markup.inline_keyboard[0][0]
    assert button.web_app.url == f"https://example.test/?plan_id={plan_id}"


async def test_group_chats_and_non_text_are_handled_politely(db):
    bot, session = make_bot()
    dp = create_dispatcher()
    await feed(dp, bot, message_update(40, "привет всем", chat_type="group"))
    assert session.calls == [] and await runs(db) == []


async def test_quick_reply_button_answers_the_question_once(db):
    bot, session = make_bot()
    dp = create_dispatcher()
    await feed(dp, bot, message_update(50, "/start"))
    async with db.sessionmaker() as s:
        chat = (await s.scalars(select(Chat))).one()
        q = Clarification(chat_id=chat.id, question="Сколько человек?", options=["Двое", "Четверо"])
        s.add(q)
        await s.commit()
        qid = q.id
    await feed(dp, bot, callback_update(51, f"clar:{qid}:1"))
    (run,) = await runs(db)
    cmd = agent_command_adapter.validate_python(run.command)
    assert cmd.type == "ProcessClarificationAnswer" and cmd.clarification_id == qid
    async with db.sessionmaker() as s:
        assert (await s.get(Clarification, qid)).answer == "Четверо"
    assert "✅ Четверо" in session.of(EditMessageText)[0].text
    # pressing it again (or an old button) is a no-op with a hint
    await feed(dp, bot, callback_update(52, f"clar:{qid}:0"))
    assert len(await runs(db)) == 1
    assert session.of(AnswerCallbackQuery)[-1].text == "Этот вопрос уже закрыт."


async def test_malformed_callback_data_is_ignored(db):
    bot, _ = make_bot()
    await feed(create_dispatcher(), bot, callback_update(60, "clar:not-a-uuid:x"))
    assert await runs(db) == []


# ---- event delivery ----

async def setup_chat(db, *, telegram_chat_id=42):
    async with db.sessionmaker() as s:
        _, chat = await ChatService(s).register(42, "ann", "Ann", "ru", telegram_chat_id)
        return chat.id


def make_delivery(bot, db):
    return EventDelivery(bot, db, get_settings(), Notifier(), max_attempts=2)


async def emit(db, chat_id, type, payload):
    async with db.sessionmaker() as s:
        await emit_event(s, chat_id, type, payload)
        await s.commit()


async def test_progress_edits_one_status_message_then_answer_replaces_it(db):
    chat_id, (bot, session) = await setup_chat(db), make_bot()
    delivery = make_delivery(bot, db)
    plan_id = uuid.uuid4()
    await emit(db, chat_id, EventType.AGENT_PROGRESS, AgentProgress(text="Ищу отели"))
    await emit(db, chat_id, EventType.AGENT_PROGRESS, AgentProgress(text="Считаю цену"))
    await emit(db, chat_id, EventType.ASSISTANT_MESSAGE, AssistantMessage(
        message_id=uuid.uuid4(), text="Готово <b>x</b>", plan_id=plan_id, revision=1))
    assert await delivery.deliver_pending() == 3
    sends, edits, deletes = session.of(SendMessage), session.of(EditMessageText), session.of(DeleteMessage)
    assert sends[0].text == "⏳ Ищу отели" and edits[0].text == "⏳ Считаю цену"
    assert len(deletes) == 1  # the status message is removed before the answer
    answer = sends[1]
    assert answer.text == "Готово &lt;b&gt;x&lt;/b&gt;"  # agent text is escaped, not interpreted as HTML
    assert answer.reply_markup.inline_keyboard[0][0].web_app.url.endswith(f"plan_id={plan_id}")
    assert await delivery.deliver_pending() == 0  # cursor advanced: nothing is sent twice


async def test_clarification_is_sent_with_quick_reply_buttons(db):
    chat_id, (bot, session) = await setup_chat(db), make_bot()
    qid = uuid.uuid4()
    await emit(db, chat_id, EventType.CLARIFICATION_REQUIRED, ClarificationRequired(
        clarification_id=qid, question="Сколько человек?", options=["Двое", "Четверо", "Семья"]))
    await make_delivery(bot, db).deliver_pending()
    msg = session.of(SendMessage)[0]
    rows = msg.reply_markup.inline_keyboard
    assert [b.text for r in rows for b in r] == ["Двое", "Четверо", "Семья"]
    assert rows[0][0].callback_data == f"clar:{qid}:0"


async def test_open_question_without_options_asks_for_a_typed_answer(db):
    chat_id, (bot, session) = await setup_chat(db), make_bot()
    await emit(db, chat_id, EventType.CLARIFICATION_REQUIRED, ClarificationRequired(
        clarification_id=uuid.uuid4(), question="Какой бюджет?"))
    await make_delivery(bot, db).deliver_pending()
    assert "Ответьте сообщением" in session.of(SendMessage)[0].text


async def test_failure_message_and_plan_ready_clean_up_progress(db):
    chat_id, (bot, session) = await setup_chat(db), make_bot()
    await emit(db, chat_id, EventType.AGENT_PROGRESS, AgentProgress(text="Ищу"))
    await emit(db, chat_id, EventType.REQUEST_FAILED, RequestFailed(reason="MCP недоступен"))
    await make_delivery(bot, db).deliver_pending()
    assert "MCP недоступен" in session.of(SendMessage)[1].text and len(session.of(DeleteMessage)) == 1


async def test_archived_and_api_only_chats_are_skipped_but_not_stuck(db):
    api_only, archived = await setup_chat(db, telegram_chat_id=None), None
    async with db.sessionmaker() as s:
        chat = await s.get(Chat, api_only)
        user = await s.get(User, chat.user_id)
        archived = (await ChatService(s).new_trip(user, 42)).id  # archives `api_only`
    bot, session = make_bot()
    await emit(db, api_only, EventType.AGENT_PROGRESS, AgentProgress(text="old"))
    await emit(db, archived, EventType.AGENT_PROGRESS, AgentProgress(text="new"))
    await make_delivery(bot, db).deliver_pending()
    assert [m.text for m in session.of(SendMessage)] == ["⏳ new"]
    async with db.sessionmaker() as s:
        assert (await s.get(Chat, api_only)).delivered_seq > 0


async def test_a_telegram_outage_is_retried_then_given_up_without_blocking_forever(db):
    chat_id, (bot, session) = await setup_chat(db), make_bot()
    delivery = make_delivery(bot, db)
    await emit(db, chat_id, EventType.AGENT_PROGRESS, AgentProgress(text="Ищу"))
    session.fail_sends = True
    assert await delivery.deliver_pending() == 0  # attempt 1: stays pending
    async with db.sessionmaker() as s:
        assert (await s.get(Chat, chat_id)).delivered_seq == 0
    assert await delivery.deliver_pending() == 1  # attempt 2 == max_attempts: dropped, cursor moves on
    session.fail_sends = False
    await emit(db, chat_id, EventType.AGENT_PROGRESS, AgentProgress(text="Дальше"))
    assert await delivery.deliver_pending() == 1 and session.of(SendMessage)[-1].text == "⏳ Дальше"


async def test_a_recovering_outage_delivers_the_event(db):
    chat_id, (bot, session) = await setup_chat(db), make_bot()
    delivery = make_delivery(bot, db)
    await emit(db, chat_id, EventType.AGENT_PROGRESS, AgentProgress(text="Ищу"))
    session.fail_sends = True
    await delivery.deliver_pending()
    session.fail_sends = False
    assert await delivery.deliver_pending() == 1 and session.of(SendMessage)[-1].text == "⏳ Ищу"


# ---- render helpers ----

def test_long_text_is_split_below_the_telegram_limit_on_boundaries():
    text = ("абзац " * 300 + "\n\n") * 5
    parts = chunks(text, 4000)
    assert len(parts) > 1 and all(len(p) <= 4000 for p in parts)
    assert "".join(p.replace("\n", "").replace(" ", "") for p in parts) == text.replace("\n", "").replace(" ", "")
    assert chunks("короткий") == ["короткий"] and chunks("") == []


# ---- webhook ----

async def test_webhook_checks_the_secret_and_feeds_the_dispatcher(db):
    bot, session = make_bot()
    app.state.chat_bot, app.state.chat_dispatcher = bot, create_dispatcher()
    try:
        body = message_update(70, "/help").model_dump(mode="json", exclude_none=True, by_alias=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            bad = await c.post("/telegram/webhook", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
            ok = await c.post("/telegram/webhook", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"})
        assert bad.status_code == 403 and ok.status_code == 200
        assert "Команды:" in session.of(SendMessage)[0].text
    finally:
        del app.state.chat_bot, app.state.chat_dispatcher
