"""Pushes chat events to Telegram.

Agents and the chat service only write rows to chat_events; this loop turns them into messages. Each chat keeps
a cursor (chats.delivered_seq), so a restart resumes where it stopped and nothing is sent twice. Progress updates
edit one "status" message instead of flooding the chat. Assumes a single process delivers events.
"""
import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select

from shared.config import Settings
from shared.database import Database
from shared.events import PAYLOADS, ChatEvent, EventType
from shared.models import Chat, ChatEvent as ChatEventRow

from ..notifier import Notifier
from . import render

log = logging.getLogger(__name__)


class EventDelivery:
    def __init__(self, bot: Bot, db: Database, settings: Settings, notifier: Notifier,
                 poll_interval: float = 1.0, max_attempts: int = 3):
        self.bot, self.db, self.settings, self.notifier = bot, db, settings, notifier
        self.poll_interval, self.max_attempts = poll_interval, max_attempts
        self._attempts: dict[int, int] = {}

    async def run(self) -> None:
        while True:
            try:
                await self.deliver_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("event delivery iteration failed")
            await self.notifier.wait(self.poll_interval)

    async def deliver_pending(self) -> int:
        """Deliver everything past each chat's cursor. Returns the number of events processed."""
        async with self.db.sessionmaker() as session:
            pending = (await session.execute(
                select(ChatEventRow.sequence, ChatEventRow.chat_id).join(Chat, Chat.id == ChatEventRow.chat_id)
                .where(ChatEventRow.sequence > Chat.delivered_seq).order_by(ChatEventRow.sequence).limit(200))).all()
        stalled: set = set()  # a failing chat must not block the others, nor skip ahead of its own failed event
        done = 0
        for sequence, chat_id in pending:
            if chat_id in stalled:
                continue
            if await self._deliver_one(sequence, chat_id) is False:
                stalled.add(chat_id)
            else:
                done += 1
        return done

    async def _deliver_one(self, sequence: int, chat_id) -> bool:
        async with self.db.sessionmaker() as session:
            chat = await session.get(Chat, chat_id)
            row = await session.get(ChatEventRow, sequence)
            if chat is None or row is None or sequence <= chat.delivered_seq:
                return True
            try:
                # Archived chats (after /new) and API-only chats have nobody to tell: just move the cursor.
                if chat.status == "active" and chat.telegram_chat_id is not None and (
                        not render.miniapp_only(self.settings) or row.type == EventType.SUGGESTIONS_PROPOSED.value):
                    await self._send(session, chat, ChatEvent.from_row(row))  # in Mini App mode the plan is shown only there
            except TelegramRetryAfter as e:
                log.warning("telegram flood control, retry in %ss", e.retry_after)
                await asyncio.sleep(min(e.retry_after, 5))
                return False
            except TelegramForbiddenError:
                log.info("user blocked the bot, dropping event %s", sequence)
            except Exception:
                n = self._attempts[sequence] = self._attempts.get(sequence, 0) + 1
                if n < self.max_attempts:
                    log.warning("delivery of event %s failed (attempt %s)", sequence, n, exc_info=True)
                    return False
                log.error("giving up on event %s after %s attempts", sequence, n, exc_info=True)
            self._attempts.pop(sequence, None)
            chat.delivered_seq = sequence
            await session.commit()
            return True

    # ---- rendering one event ----
    async def _send(self, session, chat: Chat, event: ChatEvent) -> None:
        p = PAYLOADS[event.type].model_validate(event.payload)
        t = event.type
        if t == EventType.REQUEST_QUEUED:
            text = "⏳ Принял, работаю над запросом…" if p.position == 0 else \
                f"⏳ Принял. Запросов перед этим в очереди: {p.position}."
            await self._progress(chat, text)
        elif t == EventType.AGENT_PROGRESS:
            await self._progress(chat, f"⏳ {render.esc(p.text)}")
        elif t == EventType.PLAN_UPDATED:
            await self._progress(chat, f"🗺 План обновлён (версия {p.revision}), продолжаю…")
        elif t == EventType.PLAN_READY:
            await self._clear_progress(chat)
        elif t == EventType.CLARIFICATION_REQUIRED:
            await self._clear_progress(chat)
            hint = "" if p.options else "\n\nОтветьте сообщением."
            await self.bot.send_message(chat.telegram_chat_id, f"❓ {render.esc(p.question)}{hint}",
                                        reply_markup=render.clarification_keyboard(p.clarification_id, p.options))
        elif t == EventType.ASSISTANT_MESSAGE:
            await self._clear_progress(chat)
            parts = render.chunks(render.esc(p.text) + render.map_link_text(self.settings, p.plan_id))
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                await self.bot.send_message(chat.telegram_chat_id, part,
                                            reply_markup=render.map_keyboard(self.settings, p.plan_id) if last else None)
        elif t == EventType.SUGGESTIONS_PROPOSED:
            await self.bot.send_message(
                chat.telegram_chat_id, f"💡 {render.esc(p.intro)}\nНажмите ✅, чтобы учесть, или ✖, чтобы пропустить.",
                reply_markup=render.suggestions_keyboard(p.items))
        elif t == EventType.REQUEST_FAILED:
            await self._clear_progress(chat)
            await self.bot.send_message(chat.telegram_chat_id, f"⚠️ {render.esc(p.reason)}\nМожно повторить запрос.")

    async def _progress(self, chat: Chat, text: str) -> None:
        """Show `text` in the chat's single status message (edit it, or create it)."""
        if chat.progress_message_id:
            try:
                await self.bot.edit_message_text(text, chat_id=chat.telegram_chat_id, message_id=chat.progress_message_id)
                return
            except TelegramBadRequest as e:
                if "not modified" in str(e):
                    return
                # the message is gone (deleted by the user): fall through and send a new one
        msg = await self.bot.send_message(chat.telegram_chat_id, text)
        chat.progress_message_id = msg.message_id

    async def _clear_progress(self, chat: Chat) -> None:
        if chat.progress_message_id:
            try:
                await self.bot.delete_message(chat.telegram_chat_id, chat.progress_message_id)
            except TelegramBadRequest:
                pass  # already gone
            chat.progress_message_id = None
