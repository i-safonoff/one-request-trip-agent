import pytest
from sqlalchemy import select

from chat.service import ChatService, EmptyMessage
from shared.commands import agent_command_adapter
from shared.models import AgentRun, ChatEvent, ChatMessage, Clarification


async def make_chat(db, telegram_id=42):
    async with db.sessionmaker() as s:
        user, chat = await ChatService(s).register(telegram_id, "ann", "Ann", "ru", telegram_id)
        return user, chat


async def test_register_is_idempotent_and_keeps_one_active_chat(db):
    user, chat = await make_chat(db)
    async with db.sessionmaker() as s:
        user2, chat2 = await ChatService(s).register(42, "ann2", "Ann", "ru", 42)
    assert (user2.id, chat2.id) == (user.id, chat.id) and user2.username == "ann2"


async def test_new_trip_archives_the_old_chat(db):
    user, chat = await make_chat(db)
    async with db.sessionmaker() as s:
        new = await ChatService(s).new_trip(user, 42)
    assert new.id != chat.id
    async with db.sessionmaker() as s:
        _, active = await ChatService(s).register(42, "ann", "Ann", "ru", 42)
    assert active.id == new.id


async def test_message_becomes_a_queued_run_with_an_event(db):
    _, chat = await make_chat(db)
    async with db.sessionmaker() as s:
        res = await ChatService(s).submit_text(chat, "  Москва, 6-9 ноября  ", "tg:1")
    assert res.message.text == "Москва, 6-9 ноября" and not res.duplicate and res.position == 0
    async with db.sessionmaker() as s:
        run = (await s.scalars(select(AgentRun))).one()
        cmd = agent_command_adapter.validate_python(run.command)
        event = (await s.scalars(select(ChatEvent))).one()
    assert run.status == "queued" and cmd.type == "ProcessUserMessage" and cmd.message_id == res.message.id
    assert event.type == "request.queued" and event.run_id == run.id


async def test_redelivered_message_is_processed_once(db):
    _, chat = await make_chat(db)
    async with db.sessionmaker() as s:
        first = await ChatService(s).submit_text(chat, "привет", "tg:5")
    async with db.sessionmaker() as s:
        again = await ChatService(s).submit_text(chat, "привет", "tg:5")
    assert again.duplicate and again.run is None and again.message.id == first.message.id
    async with db.sessionmaker() as s:
        assert len((await s.scalars(select(AgentRun))).all()) == 1
        assert len((await s.scalars(select(ChatMessage))).all()) == 1


async def test_second_message_reports_its_queue_position(db):
    _, chat = await make_chat(db)
    async with db.sessionmaker() as s:
        await ChatService(s).submit_text(chat, "первый", "tg:1")
    async with db.sessionmaker() as s:
        second = await ChatService(s).submit_text(chat, "второй", "tg:2")
    assert second.position == 1


async def test_empty_text_is_rejected(db):
    _, chat = await make_chat(db)
    async with db.sessionmaker() as s:
        with pytest.raises(EmptyMessage):
            await ChatService(s).submit_text(chat, "   ")


async def test_text_answers_the_open_question_instead_of_starting_a_request(db):
    _, chat = await make_chat(db)
    async with db.sessionmaker() as s:
        q = Clarification(chat_id=chat.id, question="Сколько человек?")
        s.add(q)
        await s.commit()
        qid = q.id
    async with db.sessionmaker() as s:
        res = await ChatService(s).submit_text(chat, "Двое", "tg:9")
    assert res.answered.id == qid
    async with db.sessionmaker() as s:
        run = (await s.scalars(select(AgentRun))).one()
        cmd = agent_command_adapter.validate_python(run.command)
        q = await s.get(Clarification, qid)
    assert cmd.type == "ProcessClarificationAnswer" and cmd.clarification_id == qid
    assert q.status == "answered" and q.answer == "Двое"


async def test_answer_clarification_targets_that_question_and_only_once(db):
    _, chat = await make_chat(db)
    async with db.sessionmaker() as s:
        a, b = Clarification(chat_id=chat.id, question="A?"), Clarification(chat_id=chat.id, question="B?")
        s.add_all([a, b])
        await s.commit()
        a_id, b_id = a.id, b.id
    async with db.sessionmaker() as s:
        res = await ChatService(s).answer_clarification(chat, b_id, "да", "tg-cb:1")
    assert res.answered.id == b_id
    async with db.sessionmaker() as s:
        assert (await s.get(Clarification, a_id)).status == "open"
        assert await ChatService(s).answer_clarification(chat, b_id, "ещё раз", "tg-cb:2") is None


# ---- no duplicate runs ----

async def count_runs(db):
    async with db.sessionmaker() as s:
        return len((await s.scalars(select(AgentRun))).all())


async def test_the_same_text_sent_twice_while_queued_makes_one_run(db):
    _, chat = await make_chat(db)
    async with db.sessionmaker() as s:
        first = await ChatService(s).submit_text(chat, "Сделай дешевле", "tg:1")
    async with db.sessionmaker() as s:
        second = await ChatService(s).submit_text(chat, "сделай дешевле".capitalize(), "tg:2")  # new message id
    assert second.duplicate and second.run is None and second.message.id == first.message.id
    assert await count_runs(db) == 1
    async with db.sessionmaker() as s:
        assert len((await s.scalars(select(ChatMessage))).all()) == 1  # the twin was not stored either


async def test_the_same_text_again_after_the_agents_took_it_is_a_new_request(db):
    _, chat = await make_chat(db)
    async with db.sessionmaker() as s:
        first = await ChatService(s).submit_text(chat, "Сделай дешевле", "tg:1")
    async with db.sessionmaker() as s:
        (await s.get(ChatMessage, first.message.id)).status = "done"
        await s.commit()
    async with db.sessionmaker() as s:
        again = await ChatService(s).submit_text(chat, "Сделай дешевле", "tg:2")
    assert not again.duplicate and await count_runs(db) == 2


async def test_different_texts_each_get_their_own_run_in_order(db):
    _, chat = await make_chat(db)
    for i, t in enumerate(["Москва, 6-9 ноября", "сделай дешевле", "добавь завтрак"]):
        async with db.sessionmaker() as s:
            res = await ChatService(s).submit_text(chat, t, f"tg:{i}")
        assert res.position == i
    assert await count_runs(db) == 3


async def test_two_messages_racing_for_one_question_answer_it_once_then_the_next(db, monkeypatch):
    """Both messages read the same snapshot of open questions, as concurrent handlers would."""
    from chat import repository as repo
    _, chat = await make_chat(db)
    async with db.sessionmaker() as s:
        q1, q2 = Clarification(chat_id=chat.id, question="Сколько человек?"), Clarification(chat_id=chat.id, question="Какие даты?")
        s.add_all([q1, q2])
        await s.flush()
        await s.commit()
        snapshot = await repo.open_clarifications(s, chat.id)
    assert [q.question for q in snapshot] == ["Сколько человек?", "Какие даты?"]

    async def stale(session, chat_id):
        return snapshot  # neither message sees the other's answer yet
    monkeypatch.setattr(repo, "open_clarifications", stale)

    async with db.sessionmaker() as s:
        a = await ChatService(s).submit_text(chat, "Двое", "tg:1")
    async with db.sessionmaker() as s:
        b = await ChatService(s).submit_text(chat, "С 6 по 9 ноября", "tg:2")
    assert (a.answered.question, b.answered.question) == ("Сколько человек?", "Какие даты?")
    async with db.sessionmaker() as s:
        runs = list(await s.scalars(select(AgentRun).order_by(AgentRun.created_at)))
        cmds = [agent_command_adapter.validate_python(r.command) for r in runs]
        answers = {q.question: q.answer for q in await s.scalars(select(Clarification))}
    assert [c.clarification_id for c in cmds] == [q1.id, q2.id]  # one run per question, in the order asked
    assert answers == {"Сколько человек?": "Двое", "Какие даты?": "С 6 по 9 ноября"}


async def test_a_third_message_after_all_questions_are_answered_starts_a_request(db, monkeypatch):
    from chat import repository as repo
    _, chat = await make_chat(db)
    async with db.sessionmaker() as s:
        q = Clarification(chat_id=chat.id, question="Сколько человек?")
        s.add(q)
        await s.commit()
        snapshot = await repo.open_clarifications(s, chat.id)

    async def stale(session, chat_id):
        return snapshot
    monkeypatch.setattr(repo, "open_clarifications", stale)
    async with db.sessionmaker() as s:
        await ChatService(s).submit_text(chat, "Двое", "tg:1")
    async with db.sessionmaker() as s:
        res = await ChatService(s).submit_text(chat, "и ещё бюджет 30000", "tg:2")  # the only question is gone
    assert res.answered is None
    async with db.sessionmaker() as s:
        types = [agent_command_adapter.validate_python(r.command).type
                 for r in await s.scalars(select(AgentRun).order_by(AgentRun.created_at))]
    assert types == ["ProcessClarificationAnswer", "ProcessUserMessage"]


async def test_the_same_answer_text_may_close_two_different_questions(db):
    _, chat = await make_chat(db)
    async with db.sessionmaker() as s:
        s.add_all([Clarification(chat_id=chat.id, question="Гостей?"), Clarification(chat_id=chat.id, question="Номеров?")])
        await s.commit()
    for i in (1, 2):
        async with db.sessionmaker() as s:
            res = await ChatService(s).submit_text(chat, "Два", f"tg:{i}")
        assert res.answered is not None and not res.duplicate  # an answer is never treated as a double send
    assert await count_runs(db) == 2


async def test_a_button_for_a_question_closed_in_the_meantime_creates_nothing(db):
    _, chat = await make_chat(db)
    async with db.sessionmaker() as s:
        q = Clarification(chat_id=chat.id, question="Сколько?", options=["Двое"])
        s.add(q)
        await s.commit()
        stale_q = await s.get(Clarification, q.id)
    async with db.sessionmaker() as s:
        await ChatService(s).submit_text(chat, "Двое", "tg:1")  # typed answer wins
    async with db.sessionmaker() as s:
        assert await ChatService(s).submit_text(chat, "Двое", "tg-cb:1", question=stale_q) is None
    assert await count_runs(db) == 1
    async with db.sessionmaker() as s:
        assert len((await s.scalars(select(ChatMessage))).all()) == 1  # the rejected button left no message
