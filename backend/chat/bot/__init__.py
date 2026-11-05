from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .handlers import build_router

BOT_COMMANDS = [
    BotCommand(command="plan", description="Открыть карту поездки"),
    BotCommand(command="status", description="Что сейчас происходит"),
    BotCommand(command="new", description="Начать новую поездку"),
    BotCommand(command="help", description="Подсказка"),
]


DEMO_COMMAND = BotCommand(command="demo", description="Готовый демо-план поездки")  # only in dev mode (DEV_AGENT)


def create_bot(token: str) -> Bot:
    return Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def create_dispatcher(extra_routers=()) -> Dispatcher:
    """`extra_routers`: factories of aiogram Routers from other modules (registered after the chat's own, so the chat's
    catch-all text handler would swallow their commands: they are included first)."""
    dp = Dispatcher()
    for factory in extra_routers:
        dp.include_router(factory())
    dp.include_router(build_router())
    return dp
