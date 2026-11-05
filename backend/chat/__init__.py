"""Chat module: the Telegram bot and the HTTP API for conversations.

Host usage:
    app = FastAPI(lifespan=...)          # compose chat.chat_lifespan(app) into the host's lifespan
    app.include_router(chat.router)

The module talks to the agents module only through shared.commands / shared.events and the shared tables.
"""
from .lifecycle import chat_lifespan
from .router import router

__all__ = ["router", "chat_lifespan"]
