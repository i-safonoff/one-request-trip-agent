"""Telegram part of the history module: /history, rating a stay, /profile, repeating a trip, answering suggestions.

An aiogram Router that the host adds to the chat module's dispatcher (create_dispatcher(extra_routers=...)).
Callback data (all under Telegram's 64-byte limit):
  hv:<chat>            show the trip's stays to rate          hf:<chat>:<stay#>:<-1|0|1>   rate a stay
  ha:<chat>:<stay#>:<aspect#>   toggle an aspect               hd:<chat>:<stay#>            done rating
  hr:<chat>            repeat the trip                         hp:<item>   stop suggesting an item
  hx:ask / hx:yes / hx:no   forget everything (with a confirmation step)
  sug:<suggestion>:<y|n>    accept / dismiss a suggestion (the buttons are rendered by the chat module)
"""
import logging
import uuid
from contextlib import asynccontextmanager

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from chat.bot import render
from shared.config import get_settings
from shared.database import get_database
from shared.models import User
from shared.templates import ASPECTS, parse_item

from .models import TripFeedback
from .service import HistoryError, HistoryService, NotEligible, TripEntry
from .texts import describe

log = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand(command="history", description="Прошлые поездки: оценить или повторить"),
    BotCommand(command="profile", description="Что я о вас запомнил"),
]
ASPECT_LABEL = {"price": "💰 Цена", "location": "📍 Расположение", "rating": "🧼 Сервис и чистота",
                "cancellation": "↩️ Отмена", "meal": "🍳 Питание", "stars": "⭐ Класс отеля"}
FACE = {1: "👍", 0: "😐", -1: "👎"}
NO_USER = "Сначала нажмите /start."
EMPTY_HISTORY = "Пока нет поездок с выбранными отелями. Соберите поездку, и она появится здесь."
EMPTY_PROFILE = ("Пока я ничего о вас не запомнил. Оцените отели из прошлых поездок в /history, "
                 "и я начну учитывать ваши вкусы (всегда спросив вас).")


@asynccontextmanager
async def _session():
    async with get_database().sessionmaker() as session:
        yield session


async def _user(session, tg_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id == tg_id))


def _dates(e: TripEntry) -> str:
    if not e.date_from:
        return ""
    return f"{e.date_from:%d.%m.%Y} – {e.date_to:%d.%m.%Y}" if e.date_to else f"{e.date_from:%d.%m.%Y}"


def _stay_label(stay) -> str:
    where = f", {stay.place_name}" if stay.place_name else ""
    nights = f", {stay.nights} ноч." if stay.nights else ""
    return f"«{render.esc(stay.name or 'отель')}»{render.esc(where)}{nights}"


def _trip_card(e: TripEntry) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"🧳 <b>{render.esc(e.title)}</b>", _dates(e)] + [f"• {_stay_label(s)}" for s in e.stays]
    rate = InlineKeyboardButton(text="⭐ Оценить", callback_data=f"hv:{e.chat_id}") if e.can_give_feedback \
        else InlineKeyboardButton(text="⏳ Оценка после поездки", callback_data="hn")
    return "\n".join(x for x in lines if x), InlineKeyboardMarkup(inline_keyboard=[[
        rate, InlineKeyboardButton(text="🔁 Повторить", callback_data=f"hr:{e.chat_id}")]])


async def history_cmd(message: Message):
    async with _session() as s:
        user = await _user(s, message.from_user.id)
        entries = await HistoryService(s).trips(user.id) if user else None
    if user is None:
        await message.answer(NO_USER)
    elif not entries:
        await message.answer(EMPTY_HISTORY)
    for e in (entries or [])[:5]:
        text, kb = _trip_card(e)
        await message.answer(text, reply_markup=kb)


async def _entry(svc: HistoryService, user_id, chat_id: uuid.UUID) -> TripEntry | None:
    return next((e for e in await svc.trips(user_id) if e.chat_id == chat_id), None)


async def show_stays(callback: CallbackQuery):
    chat_id = uuid.UUID(callback.data.split(":")[1])
    async with _session() as s:
        user = await _user(s, callback.from_user.id)
        entry = await _entry(HistoryService(s), user.id, chat_id) if user else None
    if entry is None:
        return await callback.answer("Поездка не найдена")
    if not entry.can_give_feedback:
        return await callback.answer("Оценить можно после поездки", show_alert=True)
    await callback.answer()
    for i, stay in enumerate(entry.stays):
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=FACE[v], callback_data=f"hf:{chat_id}:{i}:{v}") for v in (1, 0, -1)]])
        await callback.message.answer(f"Как вам {_stay_label(stay)}?", reply_markup=kb)


def _aspect_kb(chat_id, idx: int, sentiment: int, aspects: dict) -> InlineKeyboardMarkup:
    rows, row = [], []
    for n, a in enumerate(ASPECTS):
        mark = "✓ " if aspects.get(a) == sentiment else ""
        row.append(InlineKeyboardButton(text=f"{mark}{ASPECT_LABEL[a]}", callback_data=f"ha:{chat_id}:{idx}:{n}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    rows.append([InlineKeyboardButton(text="Готово", callback_data=f"hd:{chat_id}:{idx}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _rate(callback: CallbackQuery, chat_id: uuid.UUID, idx: int, sentiment: int | None, aspect: str | None):
    """Apply a rating or an aspect toggle, then redraw the message. Returns False if it was refused."""
    async with _session() as s:
        user = await _user(s, callback.from_user.id)
        svc = HistoryService(s)
        entry = await _entry(svc, user.id, chat_id) if user else None
        if entry is None or not 0 <= idx < len(entry.stays):
            await callback.answer("Поездка не найдена")
            return False
        stay = entry.stays[idx]
        current = await s.scalar(select(TripFeedback).where(
            TripFeedback.chat_id == chat_id, TripFeedback.node_id == stay.node_id))
        aspects = dict(current.aspects or {}) if current else {}
        sent = sentiment if sentiment is not None else (current.sentiment if current else 0)
        if aspect is not None:
            if sent == 0:
                await callback.answer("Сначала выберите 👍 или 👎")
                return False
            if aspects.get(aspect) == sent:
                aspects.pop(aspect)
            else:
                aspects[aspect] = sent
        elif sentiment is not None:
            aspects = {a: v for a, v in aspects.items() if v == sentiment}  # a changed mind drops contradicting reasons
        try:
            await svc.submit_feedback(user.id, chat_id, stay.node_id, sent, aspects, None)
        except NotEligible as e:
            await callback.answer(str(e), show_alert=True)
            return False
        except HistoryError as e:
            await callback.answer(str(e))
            return False
    prompt = {1: "Что понравилось?", -1: "Что не понравилось?", 0: "Принято."}[sent]
    try:
        await callback.message.edit_text(
            f"{FACE[sent]} {_stay_label(stay)}\n{prompt} (по желанию, потом «Готово»)" if sent else f"😐 {_stay_label(stay)}",
            reply_markup=_aspect_kb(chat_id, idx, sent, aspects) if sent else None)
    except TelegramBadRequest:
        pass
    await callback.answer()
    return True


async def rate_stay(callback: CallbackQuery):
    _, chat, idx, s = callback.data.split(":")
    await _rate(callback, uuid.UUID(chat), int(idx), int(s), None)


async def toggle_aspect(callback: CallbackQuery):
    _, chat, idx, n = callback.data.split(":")
    await _rate(callback, uuid.UUID(chat), int(idx), None, ASPECTS[int(n)])


async def done_rating(callback: CallbackQuery):
    await callback.answer("Спасибо, учту")
    try:
        await callback.message.edit_text(f"{callback.message.html_text}\n\n✅ Спасибо! Учту в следующих поездках.",
                                         reply_markup=None)
    except TelegramBadRequest:
        pass


async def repeat_trip(callback: CallbackQuery):
    chat_id = uuid.UUID(callback.data.split(":")[1])
    settings = get_settings()
    async with _session() as s:
        user = await _user(s, callback.from_user.id)
        if user is None:
            return await callback.answer(NO_USER)
        svc = HistoryService(s)
        try:
            template = await svc.template_for_trip(user.id, chat_id)
            chat, plan = await svc.start_from_template(user.id, template.id)
            await svc.propose(chat.id)  # what they liked before, for the user to accept or skip
        except HistoryError as e:
            return await callback.answer(str(e), show_alert=True)
    await callback.answer()
    await callback.message.answer(
        f"🔁 Начал новую поездку по образцу «{render.esc(template.name)}»: те же города и порядок, без дат и цен. "
        f"Напишите даты и пожелания.{render.map_link_text(settings, plan.id)}",
        reply_markup=render.map_keyboard(settings, plan.id))


# ---- profile ----

async def _profile_view(user_id, svc: HistoryService):
    rows = [r for r in await svc.profile(user_id) if not r.muted]
    if not rows:
        return EMPTY_PROFILE, None
    lines = ["🧠 <b>Что я о вас запомнил</b> (нажмите, чтобы больше не предлагать):"]
    buttons = []
    for r in rows[:10]:
        text = describe(parse_item(r.item))
        lines.append(f"• {render.esc(text)} ({r.observed_count} из поездок, уверенность {round(r.confidence * 100)}%)")
        buttons.append([InlineKeyboardButton(text=f"🚫 {text}"[:48], callback_data=f"hp:{r.id}")])
    buttons.append([InlineKeyboardButton(text="🗑 Забыть всё", callback_data="hx:ask")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def profile_cmd(message: Message):
    async with _session() as s:
        user = await _user(s, message.from_user.id)
        view = await _profile_view(user.id, HistoryService(s)) if user else None
    if user is None:
        return await message.answer(NO_USER)
    await message.answer(view[0], reply_markup=view[1])


async def mute_item(callback: CallbackQuery):
    item_id = uuid.UUID(callback.data.split(":")[1])
    async with _session() as s:
        user = await _user(s, callback.from_user.id)
        svc = HistoryService(s)
        try:
            await svc.set_muted(user.id, item_id, True)
        except HistoryError:
            return await callback.answer("Уже убрано")
        text, kb = await _profile_view(user.id, svc)
    await callback.answer("Больше не буду предлагать")
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


async def forget(callback: CallbackQuery):
    step = callback.data.split(":")[1]
    if step == "ask":
        await callback.answer()
        return await callback.message.answer(
            "Удалить все ваши оценки, профиль вкусов, шаблоны и предложения? Сами поездки останутся.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Да, удалить", callback_data="hx:yes"),
                InlineKeyboardButton(text="Отмена", callback_data="hx:no")]]))
    if step == "yes":
        async with _session() as s:
            user = await _user(s, callback.from_user.id)
            await HistoryService(s).forget_all(user.id)
        await callback.answer("Удалено")
        return await callback.message.edit_text("🗑 Всё удалено. Я ничего о вас не помню.", reply_markup=None)
    await callback.answer()
    await callback.message.edit_text("Хорошо, ничего не удаляю.", reply_markup=None)


# ---- suggestions from past trips ----

async def answer_suggestion(callback: CallbackQuery):
    _, raw_id, choice = callback.data.split(":")
    async with _session() as s:
        user = await _user(s, callback.from_user.id)
        row = await HistoryService(s).decide(user.id, uuid.UUID(raw_id), choice == "y") if user else None
    if row is None:
        return await callback.answer("Уже решено")
    await callback.answer("Учту ✅" if choice == "y" else "Пропускаю")
    kb = callback.message.reply_markup
    left = [r for r in (kb.inline_keyboard if kb else []) if not r[0].callback_data.startswith(f"sug:{raw_id}:")]
    try:
        if left:
            await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=left))
        else:
            await callback.message.edit_text(f"{callback.message.html_text}\n\n✓ Готово", reply_markup=None)
    except TelegramBadRequest:
        pass


async def _noop(callback: CallbackQuery):
    await callback.answer("Оценить можно после поездки")


def build_router() -> Router:
    router = Router(name="history")
    router.message.filter(F.chat.type == "private")
    router.message.register(history_cmd, Command("history"))
    router.message.register(profile_cmd, Command("profile"))
    for handler, prefix in ((show_stays, "hv:"), (rate_stay, "hf:"), (toggle_aspect, "ha:"), (done_rating, "hd:"),
                            (repeat_trip, "hr:"), (mute_item, "hp:"), (forget, "hx:"), (answer_suggestion, "sug:")):
        router.callback_query.register(handler, F.data.startswith(prefix))
    router.callback_query.register(_noop, F.data == "hn")
    return router
