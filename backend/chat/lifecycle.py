import asyncio
import logging
from contextlib import asynccontextmanager

from aiogram.types import BotCommandScopeChat, MenuButtonDefault, MenuButtonWebApp, WebAppInfo
from sqlalchemy import select
from fastapi import FastAPI

from shared.config import get_settings, refresh_public_url
from shared.database import get_database
from shared.models import Chat

from .bot import BOT_COMMANDS, DEMO_COMMAND, create_bot, create_dispatcher
from .bot.delivery import EventDelivery
from .dev_agent import DevAgent
from .notifier import notifier

log = logging.getLogger(__name__)


async def refresh_clients(bot, db, settings, commands, limit: int = 200) -> None:
    """Telegram clients cache the menu button and the command list per chat and only update them on a push for that chat:
    set both for every known private chat, so a restart or a new tunnel address reaches users who already have the chat open."""
    async with db.sessionmaker() as s:
        ids = list((await s.scalars(select(Chat.telegram_chat_id).where(Chat.telegram_chat_id.is_not(None)).distinct().limit(limit))).all())
    for chat_id in ids:
        try:
            await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=chat_id))
            if settings.public_base_url.lower().startswith("https://"):
                await bot.set_chat_menu_button(chat_id=chat_id, menu_button=MenuButtonWebApp(
                    text="🗺 Карта", web_app=WebAppInfo(url=f"{settings.public_base_url.rstrip('/')}/")))
            else:
                await bot.set_chat_menu_button(chat_id=chat_id, menu_button=MenuButtonDefault())
        except Exception as e:  # noqa: BLE001 - a blocked or deleted chat must not stop the others
            log.info("could not refresh chat %s: %s", chat_id, e)
        await asyncio.sleep(0.05)  # stay far below Telegram's rate limit


async def apply_menu_button(bot, settings) -> None:
    """The permanent "Карта" button opens the web UI as a Mini App (https only); without https the default menu is restored,
    because a Mini App button from an earlier run (a dead tunnel address) stays on the bot until it is replaced."""
    if settings.public_base_url.lower().startswith("https://"):
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
            text="🗺 Карта", web_app=WebAppInfo(url=f"{settings.public_base_url.rstrip('/')}/")))
    else:
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())


async def watch_public_url(bot, settings, db=None, commands=(), every: float = 20.0) -> None:
    """A quick tunnel gets a new address when it restarts: follow it, so the Mini App button never points at a dead one."""
    ticks = 0
    while True:
        await asyncio.sleep(every)
        ticks += 1
        try:
            changed = refresh_public_url(settings)
            # also every ~5 minutes: another copy of the bot (e.g. a teammate's dev stack) resets the menu button to the default
            if changed or ticks % 15 == 0:
                if changed:
                    log.info("public address changed to %s", settings.public_base_url)
                await apply_menu_button(bot, settings)
                if db is not None and changed:
                    await refresh_clients(bot, db, settings, list(commands))
        except Exception:  # noqa: BLE001 - a Telegram hiccup must not stop the watcher
            log.exception("could not follow the public address")


@asynccontextmanager
async def chat_lifespan(app: FastAPI, extra_bot_routers=(), extra_bot_commands=()):
    """Start the chat module's background work: the bot (webhook or long polling), event delivery, dev agent."""
    settings, db = get_settings(), get_database()
    refresh_public_url(settings)
    if settings.auto_create_tables:
        await db.create_tables()

    tasks: list[asyncio.Task] = []
    bot = None
    if settings.telegram_bot_token:
        bot, dp = create_bot(settings.telegram_bot_token), create_dispatcher(extra_bot_routers)
        app.state.chat_bot, app.state.chat_dispatcher = bot, dp
        tasks.append(asyncio.create_task(EventDelivery(bot, db, settings, notifier).run(), name="chat-delivery"))
        try:  # a Telegram outage or a bad token must not take the HTTP API down with it
            commands = [*BOT_COMMANDS, *([DEMO_COMMAND] if settings.dev_agent else []), *extra_bot_commands]
            for attempt in range(3):  # the command list and the menu button must not depend on one lucky request
                try:
                    await bot.set_my_commands(commands)
                    await apply_menu_button(bot, settings)
                    break
                except Exception:  # noqa: BLE001
                    log.warning("setting the bot menu failed (attempt %d)", attempt + 1, exc_info=attempt == 2)
                    await asyncio.sleep(2 * (attempt + 1))
            tasks.append(asyncio.create_task(refresh_clients(bot, db, settings, commands), name="chat-refresh-clients"))
            if settings.public_url_file:
                tasks.append(asyncio.create_task(watch_public_url(bot, settings, db, commands), name="chat-public-url"))
            if settings.telegram_webhook_url:
                await bot.set_webhook(settings.telegram_webhook_url, secret_token=settings.telegram_webhook_secret,
                                      allowed_updates=dp.resolve_used_update_types())
            else:  # local development: no public URL, so pull updates
                await bot.delete_webhook()
                tasks.append(asyncio.create_task(dp.start_polling(bot, handle_signals=False), name="chat-polling"))
        except Exception:
            log.exception("could not start the Telegram bot; the HTTP API keeps running")
    else:
        log.warning("TELEGRAM_BOT_TOKEN is not set: the Telegram bot is disabled, only the HTTP API works")
    if settings.dev_agent:
        log.warning("dev agent enabled: runs are answered with mock data")
        tasks.append(asyncio.create_task(DevAgent(db).run(), name="chat-dev-agent"))

    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if bot is not None:
            await bot.session.close()
