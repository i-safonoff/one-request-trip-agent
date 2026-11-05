"""Trip history: feedback on past trips, a learned (and visible, deletable) profile of tastes, suggestions for a new
trip, and templates ("repeat this trip"). Depends on `shared`, and on `chat` for identity (session tokens) only.

Host usage:
    app.include_router(history.router)
    chat.chat_lifespan(app, extra_bot_routers=[history.build_bot_router], extra_bot_commands=history.BOT_COMMANDS)
    chat.hooks.on_new_trip.append(history.propose_on_new_trip)
Design and business review: docs/trip-templates.md
"""
from .bot import BOT_COMMANDS, build_router as build_bot_router
from .router import router
from .service import HistoryService


async def propose_on_new_trip(session, user, chat) -> None:
    """chat.hooks.on_new_trip hook: offer what the user liked in past trips for the trip that just started."""
    await HistoryService(session).propose(chat.id)


__all__ = ["router", "build_bot_router", "BOT_COMMANDS", "propose_on_new_trip", "HistoryService"]
