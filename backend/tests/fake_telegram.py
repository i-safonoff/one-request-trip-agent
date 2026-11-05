"""A Bot whose HTTP session records every API call instead of talking to Telegram."""
from datetime import datetime

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update

from .conftest import TEST_BOT_TOKEN


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list = []
        self.fail_sends = False
        self._next_id = 1000

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            if self.fail_sends:
                raise RuntimeError("telegram is down")
            self._next_id += 1
            return Message(message_id=self._next_id, date=datetime.now(), chat=Chat(id=method.chat_id, type="private"),
                           text=method.text)
        return True

    def of(self, kind):
        return [c for c in self.calls if isinstance(c, kind)]


def make_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    return Bot(TEST_BOT_TOKEN, session=session), session


def message_update(update_id: int, text: str, *, user_id=42, chat_type="private", message_id=None) -> Update:
    entities = []
    if text.startswith("/"):
        entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return Update.model_validate({"update_id": update_id, "message": {
        "message_id": message_id or update_id, "date": 1_700_000_000, "chat": {"id": user_id, "type": chat_type},
        "from": {"id": user_id, "is_bot": False, "first_name": "Ann", "username": "ann"},
        "text": text, "entities": entities}})


def callback_update(update_id: int, data: str, *, user_id=42, text="❓ Вопрос", keyboard=None) -> Update:
    """`keyboard`: rows of (text, callback_data) the pressed message carries, if the handler inspects them."""
    message = {"message_id": 7, "date": 1_700_000_000, "chat": {"id": user_id, "type": "private"},
               "text": text, "from": {"id": 1, "is_bot": True, "first_name": "bot"}}
    if keyboard:
        message["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in keyboard]}
    return Update.model_validate({"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "chat_instance": "ci", "data": data,
        "from": {"id": user_id, "is_bot": False, "first_name": "Ann"}, "message": message}})
