"""Telegram handlers. They only translate chat events into ChatService calls; replies to the user
(progress, questions, answers) come back asynchronously through chat_events (see delivery.py)."""
import logging
import uuid
from contextlib import asynccontextmanager

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message, User as TgUser

from shared.config import get_settings
from shared.database import get_database

from .. import repository as repo
from .. import mock_plans
from ..service import ChatService, EmptyMessage
from . import render

log = logging.getLogger(__name__)

@asynccontextmanager
async def _service():
    async with get_database().sessionmaker() as session:
        yield ChatService(session), session


async def _register(svc: ChatService, tg_user: TgUser, tg_chat_id: int):
    return await svc.register(tg_user.id, tg_user.username, tg_user.first_name, tg_user.language_code, tg_chat_id)


async def start(message: Message):
    async with _service() as (svc, _):
        await _register(svc, message.from_user, message.chat.id)
    settings = get_settings()
    if render.miniapp_only(settings):
        await message.answer(render.START_TEXT_APP, reply_markup=render.app_keyboard(settings))
        return
    await message.answer(render.START_TEXT)


async def help_(message: Message):
    settings = get_settings()
    if render.miniapp_only(settings):
        await message.answer(render.HELP_TEXT_APP, reply_markup=render.app_keyboard(settings))
        return
    await message.answer(render.HELP_TEXT)


async def new_trip(message: Message):
    async with _service() as (svc, _):
        user, _chat = await _register(svc, message.from_user, message.chat.id)
        await svc.new_trip(user, message.chat.id)
    settings = get_settings()
    if render.miniapp_only(settings):
        await message.answer("Новая поездка создана. Откройте приложение и опишите её там.", reply_markup=render.app_keyboard(settings))
        return
    await message.answer(render.NEW_TRIP)


async def open_app(message: Message):
    """Mini App mode: the bot never plans in the chat, so Telegram and the app cannot show different things."""
    async with _service() as (svc, _):
        await _register(svc, message.from_user, message.chat.id)
    await message.answer(render.APP_REDIRECT, reply_markup=render.app_keyboard(get_settings()))


def _in_miniapp_mode(message: Message) -> bool:
    return render.miniapp_only(get_settings())


async def plan(message: Message):
    async with _service() as (svc, _):
        _user, chat = await _register(svc, message.from_user, message.chat.id)
        trip = await svc.plan(chat)
    if trip is None:
        await message.answer(render.NO_PLAN)
        return
    settings = get_settings()
    title = (trip.view_json or {}).get("title") or "Ваша поездка"
    await message.answer(
        f"🗺 <b>{render.esc(str(title))}</b>\nВерсия плана: {trip.revision}{render.map_link_text(settings, trip.id)}",
        reply_markup=render.map_keyboard(settings, trip.id))


async def status(message: Message):
    async with _service() as (svc, _):
        _user, chat = await _register(svc, message.from_user, message.chat.id)
        st = await svc.status(chat)
    lines = []
    if st.open_question:
        lines.append(f"❓ Жду ответа: {render.esc(st.open_question.question)}")
    if st.active_runs:
        lines.append(f"⏳ Запросов в обработке или в очереди: {st.active_runs}")
    if not lines:
        lines.append("Сейчас ничего не обрабатывается." + (" План готов: /plan" if st.has_plan else " Опишите поездку."))
    await message.answer("\n".join(lines))


async def demo(message: Message):
    """Instant mock plan (DEV_AGENT mode): `/demo` is Moscow + St Petersburg, `/demo japan` is Tokyo + Osaka."""
    if not get_settings().dev_agent:
        await message.answer(render.DEMO_OFF)
        return
    scenario = mock_plans.parse_scenario((message.text or "").partition(" ")[2])
    if scenario is None:
        await message.answer(render.DEMO_USAGE)
        return
    async with _service() as (svc, session):
        _user, chat = await _register(svc, message.from_user, message.chat.id)
        await mock_plans.add_demo_plan(session, chat, scenario)
    # the plan is announced through chat_events like a real agent's answer


async def unknown_command(message: Message):
    await message.answer(render.UNKNOWN_COMMAND)


async def text(message: Message):
    async with _service() as (svc, _):
        _user, chat = await _register(svc, message.from_user, message.chat.id)
        try:
            await svc.submit_text(chat, message.text, external_id=f"tg:{message.message_id}")
        except EmptyMessage:
            return
    # No reply here on purpose: the agents' answer, progress and questions arrive as chat events.


async def clarification_button(callback: CallbackQuery):
    try:
        _, raw_id, raw_idx = callback.data.split(":")
        clarification_id, idx = uuid.UUID(raw_id), int(raw_idx)
    except ValueError:
        await callback.answer()
        return
    chosen = None
    async with _service() as (svc, session):
        _user, chat = await _register(svc, callback.from_user, callback.from_user.id)
        question = await repo.get_clarification(session, chat.id, clarification_id)
        if question and question.status == "open" and question.options and 0 <= idx < len(question.options):
            chosen = question.options[idx]
            await svc.answer_clarification(chat, question.id, chosen, external_id=f"tg-cb:{callback.id}")
    if chosen is None:
        await callback.answer(render.QUESTION_CLOSED)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        try:  # drop the buttons and record the choice under the question
            await callback.message.edit_text(f"{callback.message.html_text}\n\n✅ {render.esc(chosen)}", reply_markup=None)
        except TelegramBadRequest:
            log.debug("could not edit the question message", exc_info=True)


async def other_content(message: Message):
    await message.answer(render.ONLY_TEXT)


def build_router() -> Router:
    """A fresh Router per Dispatcher (aiogram routers can be attached only once). Order matters: first match wins."""
    router = Router(name="chat")
    router.message.filter(F.chat.type == "private")  # the bot plans one person's trip, groups are ignored
    router.message.register(start, CommandStart())
    router.message.register(help_, Command("help"))
    router.message.register(new_trip, Command("new"))
    router.message.register(open_app, Command("plan"), _in_miniapp_mode)
    router.message.register(open_app, Command("status"), _in_miniapp_mode)
    router.message.register(plan, Command("plan"))
    router.message.register(status, Command("status"))
    router.message.register(demo, Command("demo"))
    router.message.register(unknown_command, F.text.startswith("/"))
    router.message.register(open_app, F.text, _in_miniapp_mode)
    router.message.register(text, F.text)
    router.callback_query.register(clarification_button, F.data.startswith("clar:"))
    router.message.register(other_content)
    return router
