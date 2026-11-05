"""HTTP API of the chat module (Mini App and any other client) plus the Telegram webhook."""
import asyncio
import time
import uuid
from datetime import datetime
from typing import AsyncIterator, Awaitable, Callable

from aiogram.types import Update
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import get_settings
from shared.database import Database, get_database, get_session
from shared.events import ChatEvent, events_after
from shared.models import Chat, TripPlan, TripPlanVersion, User

from . import repository as repo
from .auth import InvalidInitData, current_user, issue_token, validate_init_data
from .schemas import (AnswerIn, AuthOut, ChatOut, MessageIn, MessageOut, PlanOut, SubmittedOut, TelegramAuthIn,
                      UserOut)
from .service import ChatService

router = APIRouter()
api = APIRouter(prefix="/api/v1", tags=["chat"])


async def _owned_chat(chat_id: uuid.UUID, user: User, session: AsyncSession) -> Chat:
    chat = await repo.get_user_chat(session, chat_id, user.id)
    if chat is None:
        raise HTTPException(404, "Chat not found")
    return chat


def _plan_out(plan: TripPlan | TripPlanVersion, status: str, plan_id: uuid.UUID, updated_at: datetime) -> PlanOut:
    return PlanOut(plan_id=plan_id, revision=plan.revision, status=status, view=plan.view_json or {},
                   updated_at=updated_at)


@api.post("/auth/telegram", response_model=AuthOut)
async def auth_telegram(body: TelegramAuthIn, session: AsyncSession = Depends(get_session)):
    """Exchange Mini App initData for a session token. The user id is taken from the signed data only."""
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise HTTPException(503, "Telegram bot token is not configured")
    try:
        tg = validate_init_data(body.init_data, settings.telegram_bot_token, settings.init_data_max_age_seconds)
    except InvalidInitData as e:
        raise HTTPException(401, f"Invalid initData: {e}")
    user, _chat = await ChatService(session).register(
        int(tg["id"]), tg.get("username"), tg.get("first_name"), tg.get("language_code"), int(tg["id"]))
    return AuthOut(token=issue_token(user.id, settings), user=UserOut.model_validate(user))


@api.get("/chats/active", response_model=ChatOut)
async def active_chat(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """The chat the bot is currently working on for this user (created if missing)."""
    return await ChatService(session).active_for(user)


@api.post("/chats", response_model=ChatOut, status_code=201)
async def create_chat(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Start a new trip: archives the current chat."""
    return await ChatService(session).new_trip(user, user.telegram_id)


@api.get("/chats/{chat_id}/messages", response_model=list[MessageOut])
async def list_messages(chat_id: uuid.UUID, limit: int = Query(50, ge=1, le=200), before: datetime | None = None,
                        user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    chat = await _owned_chat(chat_id, user, session)
    return await repo.list_messages(session, chat.id, limit, before)


@api.post("/chats/{chat_id}/messages", response_model=SubmittedOut, status_code=202)
async def post_message(chat_id: uuid.UUID, body: MessageIn, user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    """Accept a message; the answer arrives as events (SSE / Telegram). A pending question takes it as its answer."""
    chat = await _owned_chat(chat_id, user, session)
    ext = f"api:{body.client_message_id}" if body.client_message_id else None
    res = await ChatService(session).submit_text(chat, body.text, ext)
    return SubmittedOut(message_id=res.message.id, run_id=res.run.id if res.run else None,
                        duplicate=res.duplicate, position=res.position)


@api.post("/chats/{chat_id}/clarifications/{question_id}/answers", response_model=SubmittedOut, status_code=202)
async def answer_clarification(chat_id: uuid.UUID, question_id: uuid.UUID, body: AnswerIn,
                               user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    chat = await _owned_chat(chat_id, user, session)
    ext = f"api:{body.client_message_id}" if body.client_message_id else None
    res = await ChatService(session).answer_clarification(chat, question_id, body.answer, ext)
    if res is None:
        raise HTTPException(409, "The question does not exist or is already answered")
    return SubmittedOut(message_id=res.message.id, run_id=res.run.id if res.run else None,
                        duplicate=res.duplicate, position=res.position)


@api.get("/chats/{chat_id}/plan", response_model=PlanOut)
async def chat_plan(chat_id: uuid.UUID, user: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session)):
    chat = await _owned_chat(chat_id, user, session)
    plan = await repo.latest_plan(session, chat.id)
    if plan is None:
        raise HTTPException(404, "No plan yet")
    return _plan_out(plan, plan.status, plan.id, plan.updated_at)


@api.get("/plans/{plan_id}/versions/{revision}", response_model=PlanOut)
async def plan_version(plan_id: uuid.UUID, revision: int, user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    plan = await session.get(TripPlan, plan_id)
    if plan is None or await repo.get_user_chat(session, plan.chat_id, user.id) is None:
        raise HTTPException(404, "Plan not found")
    if revision == plan.revision:
        return _plan_out(plan, plan.status, plan.id, plan.updated_at)
    version = await session.get(TripPlanVersion, (plan_id, revision))
    if version is None:
        raise HTTPException(404, "Revision not found")
    return _plan_out(version, "snapshot", plan.id, version.created_at)


# ---- events (SSE) ----

def _sse(event: ChatEvent) -> str:
    return f"id: {event.sequence}\nevent: {event.type.value}\ndata: {event.model_dump_json()}\n\n"


async def event_stream(db: Database, chat_id: uuid.UUID, after: int,
                       is_disconnected: Callable[[], Awaitable[bool]] | None = None,
                       poll_interval: float = 1.0, heartbeat: float = 15.0,
                       max_seconds: float | None = None) -> AsyncIterator[str]:
    """Events past `after`, then new ones as they commit. A reconnecting client resumes from its last id."""
    last, started, last_sent = after, time.monotonic(), time.monotonic()
    while True:
        async with db.sessionmaker() as session:
            events = await events_after(session, chat_id, last)
        for event in events:
            yield _sse(event)
            last, last_sent = event.sequence, time.monotonic()
        if is_disconnected and await is_disconnected():
            return
        if max_seconds is not None and time.monotonic() - started >= max_seconds:
            return
        if time.monotonic() - last_sent >= heartbeat:
            yield ": keep-alive\n\n"  # keeps proxies from closing an idle stream
            last_sent = time.monotonic()
        await asyncio.sleep(poll_interval)


@api.get("/chats/{chat_id}/events")
async def chat_events(chat_id: uuid.UUID, request: Request, after: int | None = Query(None, ge=0),
                      last_event_id: str | None = Header(default=None),
                      user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """Server-Sent Events. Resume with ?after=<sequence> or the standard Last-Event-ID header."""
    chat = await _owned_chat(chat_id, user, session)
    if after is None:
        after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0
    return StreamingResponse(
        event_stream(get_database(), chat.id, after, request.is_disconnected),
        media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- Telegram webhook ----

@router.post("/telegram/webhook", include_in_schema=False)
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    settings = get_settings()
    bot, dp = getattr(request.app.state, "chat_bot", None), getattr(request.app.state, "chat_dispatcher", None)
    if bot is None or dp is None:
        raise HTTPException(503, "Bot is not running")
    if settings.telegram_webhook_secret and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(403, "Bad secret token")
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)  # handler errors are logged by aiogram; Telegram must still get a 200
    return Response(status_code=200)


router.include_router(api)
