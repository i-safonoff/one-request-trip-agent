"""Host: assembles the modules into one FastAPI backend.

    chat      Telegram bot + HTTP API of the dialogue (users, chats, messages, SSE)
    history   trip history, feedback, learned preferences, trip templates
    agents    the agents' HTTP side (guest sign-in, UI actions), cart and simulated checkout, the web UI at `/`
    trip_map  road routes and map adapters (plans are read from the database, see integration.py)

The agents WORKER is a separate process (`python -m agents.runtime.worker`, a docker-compose service).
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

import agents.api
import agents.shop.router
import chat
import history
import integration
import trip_map
from agents.config import get_settings as agents_settings
from agents.services import make_providers
from shared.config import get_settings
from shared.database import get_database

STATIC = Path(__file__).parent / "agents" / "static"
log = logging.getLogger("host")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = agents_settings()
    # data sources are only needed here to re-check prices at the cart; the worker builds its own
    app.state.hotels, app.state.transport, mcp = make_providers(s)
    log.info("sources: hotels=%s transport=%s", s.hotels_source, s.transport_source)
    try:
        async with chat.chat_lifespan(app, extra_bot_routers=[history.build_bot_router],
                                      extra_bot_commands=history.BOT_COMMANDS):
            yield
    finally:
        if mcp:
            await mcp.aclose()


app = FastAPI(title="ekvivalentno", version="1.0", lifespan=lifespan)
app.include_router(chat.router)
app.include_router(history.router)
app.include_router(agents.api.router)
app.include_router(agents.shop.router.router)
app.include_router(trip_map.router)
trip_map.mount_static(app)
integration.wire(app)  # the map reads plans of the signed-in user from the database


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok"}


@app.get("/ready", tags=["ops"])
async def ready():
    """The database must be up; LLM key and data sources are reported but do not fail readiness."""
    s = agents_settings()
    out = {"status": "ready", "llm": "configured" if s.gigachat_auth_key else "missing GIGACHAT_AUTH_KEY",
           "hotels": s.hotels_source, "transport": s.transport_source,
           "telegram": "configured" if get_settings().telegram_bot_token else "disabled (no TELEGRAM_BOT_TOKEN)"}
    try:
        async with get_database().sessionmaker() as session:
            await session.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({**out, "status": "error", "db": str(e)}, status_code=503)
    return out


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
