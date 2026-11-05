"""Extension points other modules plug into without the chat module importing them."""
import logging
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Chat, User

log = logging.getLogger(__name__)

# Called after a new trip's chat is created (/new, API): hook(session, user, chat). The session is fresh and the hook
# commits its own work. A failing hook is logged and never breaks starting a trip.
on_new_trip: list[Callable[[AsyncSession, User, Chat], Awaitable[None]]] = []


async def run_new_trip_hooks(session: AsyncSession, user: User, chat: Chat) -> None:
    for hook in list(on_new_trip):
        try:
            await hook(session, user, chat)
        except Exception:
            await session.rollback()
            log.exception("new-trip hook %r failed", getattr(hook, "__name__", hook))
