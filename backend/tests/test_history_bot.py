"""The history module's Telegram side, driven through the real dispatcher with a recording Bot."""
import uuid

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, SendMessage
from sqlalchemy import select

import history
import main  # noqa: F401  (registers the new-trip hook, as the host does)
from chat.bot import create_dispatcher
from chat.bot.delivery import EventDelivery
from chat.notifier import Notifier
from shared.config import get_settings
from shared.models import Chat, InheritedItem, TripPlan
from history.models import PreferenceItem, TripFeedback

from .fake_telegram import callback_update, make_bot, message_update
from .test_history_service import seed_trip, seed_user


@pytest.fixture
def early(db, monkeypatch):
    monkeypatch.setenv("HISTORY_ALLOW_EARLY_FEEDBACK", "true")
    get_settings.cache_clear()


def dispatcher():
    return create_dispatcher([history.build_bot_router])


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


async def setup(db, title="Токио и Осака", tg=42):
    uid = await seed_user(db, tg)
    return uid, await seed_trip(db, uid, title=title, status="archived")


async def test_history_shows_trips_with_rate_and_repeat_buttons(db, early):
    uid, chat = await setup(db)
    bot, session = make_bot()
    await dispatcher().feed_update(bot, message_update(1, "/history"))
    msg = session.of(SendMessage)[0]
    assert "Токио и Осака" in msg.text and "«Cross Hotel Osaka», Осака, 4 ноч." in msg.text and "12.11.2026 – 21.11.2026" in msg.text
    assert [b.callback_data for b in buttons(msg.reply_markup)] == [f"hv:{chat}", f"hr:{chat}"]


async def test_history_before_the_trip_ends_offers_no_rating(db, monkeypatch):
    monkeypatch.delenv("HISTORY_ALLOW_EARLY_FEEDBACK", raising=False)
    get_settings.cache_clear()
    await setup(db)
    bot, session = make_bot()
    await dispatcher().feed_update(bot, message_update(1, "/history"))
    assert "Оценка после поездки" in buttons(session.of(SendMessage)[0].reply_markup)[0].text


async def test_history_without_trips_or_without_start_is_polite(db, early):
    bot, session = make_bot()
    await dispatcher().feed_update(bot, message_update(1, "/history"))
    assert "Сначала нажмите /start" in session.of(SendMessage)[0].text
    await seed_user(db)
    await dispatcher().feed_update(bot, message_update(2, "/history"))
    assert "Пока нет поездок" in session.of(SendMessage)[1].text


async def test_rating_a_stay_with_reasons_through_buttons(db, early):
    uid, chat = await setup(db)
    bot, session = make_bot()
    dp = dispatcher()
    await dp.feed_update(bot, callback_update(1, f"hv:{chat}"))
    asks = session.of(SendMessage)
    assert len(asks) == 2 and "Shinjuku Granbell Hotel" in asks[0].text and "Cross Hotel Osaka" in asks[1].text
    assert [b.callback_data for b in buttons(asks[1].reply_markup)] == [f"hf:{chat}:1:1", f"hf:{chat}:1:0", f"hf:{chat}:1:-1"]

    await dp.feed_update(bot, callback_update(2, f"hf:{chat}:1:1"))  # 👍 on Osaka
    edit = session.of(EditMessageText)[-1]
    assert "Что понравилось?" in edit.text
    labels = [b.text for b in buttons(edit.reply_markup)]
    assert "🍳 Питание" in labels and "Готово" in labels
    meal = next(b for b in buttons(edit.reply_markup) if b.text == "🍳 Питание").callback_data
    await dp.feed_update(bot, callback_update(3, meal))
    assert "✓ 🍳 Питание" in [b.text for b in buttons(session.of(EditMessageText)[-1].reply_markup)]
    async with db.sessionmaker() as s:
        fb = (await s.scalars(select(TripFeedback))).one()
        assert (fb.node_id, fb.sentiment, fb.aspects) == ("stay_osaka", 1, {"meal": 1})
        assert "constraint:breakfast" in {p.item_key for p in await s.scalars(select(PreferenceItem))}
    await dp.feed_update(bot, callback_update(4, meal))  # pressing again takes the reason back
    async with db.sessionmaker() as s:
        assert (await s.scalars(select(TripFeedback))).one().aspects == {}
    await dp.feed_update(bot, callback_update(5, f"hd:{chat}:1"))
    assert "Спасибо" in session.of(EditMessageText)[-1].text


async def test_changing_the_rating_drops_reasons_that_no_longer_fit(db, early):
    uid, chat = await setup(db)
    bot, session = make_bot()
    dp = dispatcher()
    await dp.feed_update(bot, callback_update(1, f"hf:{chat}:1:1"))
    await dp.feed_update(bot, callback_update(2, f"ha:{chat}:1:{__import__('shared.templates', fromlist=['ASPECTS']).ASPECTS.index('location')}"))
    await dp.feed_update(bot, callback_update(3, f"hf:{chat}:1:-1"))  # changed their mind: 👎
    async with db.sessionmaker() as s:
        fb = (await s.scalars(select(TripFeedback))).one()
    assert fb.sentiment == -1 and fb.aspects == {}  # "liked the location" does not survive a 👎


async def test_a_reason_needs_a_thumb_first(db, early):
    uid, chat = await setup(db)
    bot, session = make_bot()
    await dispatcher().feed_update(bot, callback_update(1, f"ha:{chat}:1:0"))
    assert "Сначала выберите" in session.of(AnswerCallbackQuery)[-1].text
    async with db.sessionmaker() as s:
        assert (await s.scalars(select(TripFeedback))).all() == []


async def test_stale_or_foreign_buttons_do_nothing(db, early):
    uid, chat = await setup(db, tg=42)
    await seed_user(db, 99)
    bot, session = make_bot()
    dp = dispatcher()
    await dp.feed_update(bot, callback_update(1, f"hf:{chat}:1:1", user_id=99))  # someone else's trip
    await dp.feed_update(bot, callback_update(2, f"hf:{chat}:7:1"))  # a stay that does not exist
    await dp.feed_update(bot, callback_update(3, f"hf:{uuid.uuid4()}:0:1"))
    async with db.sessionmaker() as s:
        assert (await s.scalars(select(TripFeedback))).all() == []
    assert all("не найдена" in a.text for a in session.of(AnswerCallbackQuery)[-3:])


async def test_profile_shows_what_was_learned_and_lets_the_user_remove_it(db, early):
    uid, chat = await setup(db)
    bot, session = make_bot()
    dp = dispatcher()
    await dp.feed_update(bot, message_update(1, "/profile"))
    assert "Пока я ничего о вас не запомнил" in session.of(SendMessage)[-1].text
    await dp.feed_update(bot, callback_update(2, f"hf:{chat}:1:1"))
    await dp.feed_update(bot, message_update(3, "/profile"))
    msg = session.of(SendMessage)[-1]
    assert "Что я о вас запомнил" in msg.text and "Вы уже были в «Cross Hotel Osaka»" in msg.text
    mute = next(b for b in buttons(msg.reply_markup) if b.callback_data.startswith("hp:"))
    await dp.feed_update(bot, callback_update(4, mute.callback_data))
    assert "Больше не буду" in session.of(AnswerCallbackQuery)[-1].text
    async with db.sessionmaker() as s:
        assert [p.muted for p in await s.scalars(select(PreferenceItem))].count(True) == 1


async def test_forgetting_everything_asks_first(db, early):
    uid, chat = await setup(db)
    bot, session = make_bot()
    dp = dispatcher()
    await dp.feed_update(bot, callback_update(1, f"hf:{chat}:1:1"))
    await dp.feed_update(bot, callback_update(2, "hx:ask"))
    assert "Удалить все" in session.of(SendMessage)[-1].text
    async with db.sessionmaker() as s:
        assert (await s.scalars(select(TripFeedback))).all()  # asking deletes nothing
    await dp.feed_update(bot, callback_update(3, "hx:no"))
    async with db.sessionmaker() as s:
        assert (await s.scalars(select(TripFeedback))).all()
    await dp.feed_update(bot, callback_update(4, "hx:yes"))
    async with db.sessionmaker() as s:
        assert (await s.scalars(select(TripFeedback))).all() == []
        assert (await s.scalars(select(TripPlan))).all()  # trips stay


async def test_repeat_creates_a_draft_trip_and_offers_what_was_liked(db, early):
    uid, chat = await setup(db)
    bot, session = make_bot()
    dp = dispatcher()
    await dp.feed_update(bot, callback_update(1, f"hf:{chat}:1:1"))
    await dp.feed_update(bot, callback_update(2, f"ha:{chat}:1:{4}"))  # aspect 4 = meal
    await dp.feed_update(bot, callback_update(3, f"hr:{chat}"))
    msg = session.of(SendMessage)[-1]
    assert "по образцу «Токио и Осака»" in msg.text and "без дат и цен" in msg.text
    async with db.sessionmaker() as s:
        chats = list(await s.scalars(select(Chat).order_by(Chat.created_at)))
        assert [c.status for c in chats] == ["archived", "active"]
        draft = await s.scalar(select(TripPlan).where(TripPlan.chat_id == chats[1].id))
        assert draft.status == "draft" and msg.reply_markup.inline_keyboard[0][0].web_app.url.endswith(f"plan_id={draft.id}")
        offered = list(await s.scalars(select(InheritedItem).where(InheritedItem.chat_id == chats[1].id)))
    assert offered and all(i.status == "proposed" for i in offered)  # proposed, never applied silently


async def test_new_trip_proposes_past_tastes_and_the_answer_buttons_work(db, early):
    uid, chat = await setup(db)
    bot, session = make_bot()
    dp = dispatcher()
    await dp.feed_update(bot, callback_update(1, f"hf:{chat}:1:1"))
    await dp.feed_update(bot, callback_update(2, f"ha:{chat}:1:4"))  # meal
    await dp.feed_update(bot, callback_update(21, f"ha:{chat}:1:1"))  # location: a second suggestion
    await dp.feed_update(bot, message_update(3, "/new"))
    await EventDelivery(bot, db, get_settings(), Notifier()).deliver_pending()
    ask = next(m for m in session.of(SendMessage) if m.text.startswith("💡"))
    assert "Нажмите ✅" in ask.text
    rows = [[(b.text, b.callback_data) for b in row] for row in ask.reply_markup.inline_keyboard]
    accept = next(r for r in rows if "Завтрак включён" in r[0][0])[0][1]
    assert accept.startswith("sug:") and accept.endswith(":y")
    await dp.feed_update(bot, callback_update(4, accept, keyboard=rows))
    async with db.sessionmaker() as s:
        row = await s.scalar(select(InheritedItem).where(InheritedItem.item_key == "constraint:breakfast"))
        assert row.status == "accepted"
    left = session.of(EditMessageReplyMarkup)[-1].reply_markup
    texts = " ".join(b.text for b in buttons(left))
    assert "Завтрак включён" not in texts and "Хорошее расположение" in texts  # that row is gone, the other stays
    await dp.feed_update(bot, callback_update(5, accept, keyboard=rows))  # a second tap is harmless
    assert "Уже решено" in session.of(AnswerCallbackQuery)[-1].text


async def test_new_trip_without_history_proposes_nothing(db, early):
    await seed_user(db)
    bot, session = make_bot()
    await dispatcher().feed_update(bot, message_update(1, "/start"))
    await dispatcher().feed_update(bot, message_update(2, "/new"))
    await EventDelivery(bot, db, get_settings(), Notifier()).deliver_pending()
    assert not [m for m in session.of(SendMessage) if m.text.startswith("💡")]
