"""Plain queries for the chat module. No business rules here: see service.py."""
import uuid
from datetime import datetime, timezone

from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import AgentRun, Chat, ChatMessage, Clarification, TripPlan, User

ACTIVE_RUN_STATUSES = ("queued", "running")


async def upsert_user(session: AsyncSession, telegram_id: int, username: str | None,
                      first_name: str | None, language_code: str | None) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(telegram_id=telegram_id)
        session.add(user)
    user.username, user.first_name, user.language_code = username, first_name, language_code
    await session.flush()
    return user


async def active_chat(session: AsyncSession, user_id: uuid.UUID) -> Chat | None:
    return await session.scalar(
        select(Chat).where(Chat.user_id == user_id, Chat.status == "active").order_by(Chat.created_at.desc()))


async def get_user_chat(session: AsyncSession, chat_id: uuid.UUID, user_id: uuid.UUID) -> Chat | None:
    return await session.scalar(select(Chat).where(Chat.id == chat_id, Chat.user_id == user_id))


async def archive_active_chats(session: AsyncSession, user_id: uuid.UUID) -> None:
    for chat in await session.scalars(select(Chat).where(Chat.user_id == user_id, Chat.status == "active")):
        chat.status = "archived"


async def add_message(session: AsyncSession, chat_id: uuid.UUID, role: str, text: str, *,
                      status: str | None = None, external_id: str | None = None) -> tuple[ChatMessage, bool]:
    """Insert a message. Returns (message, created); created=False when external_id was already seen."""
    if external_id:
        existing = await session.scalar(
            select(ChatMessage).where(ChatMessage.chat_id == chat_id, ChatMessage.external_id == external_id))
        if existing:
            return existing, False
    msg = ChatMessage(chat_id=chat_id, role=role, text=text, status=status, external_id=external_id)
    try:
        async with session.begin_nested():  # a concurrent redelivery may win the race: the savepoint absorbs it
            session.add(msg)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            select(ChatMessage).where(ChatMessage.chat_id == chat_id, ChatMessage.external_id == external_id))
        if existing is None:
            raise
        return existing, False
    return msg, True


async def find_message(session: AsyncSession, chat_id: uuid.UUID, external_id: str) -> ChatMessage | None:
    return await session.scalar(
        select(ChatMessage).where(ChatMessage.chat_id == chat_id, ChatMessage.external_id == external_id))


async def recent_identical_queued(session: AsyncSession, chat_id: uuid.UUID, text: str, window_seconds: int,
                                  exclude_id: uuid.UUID) -> ChatMessage | None:
    """An earlier identical user message that the agents have not started on yet (a double send)."""
    return await session.scalar(
        select(ChatMessage).where(
            ChatMessage.chat_id == chat_id, ChatMessage.role == "user", ChatMessage.status == "queued",
            ChatMessage.clarification_id.is_(None), ChatMessage.text == text, ChatMessage.id != exclude_id,
            ChatMessage.created_at >= utcnow() - timedelta(seconds=window_seconds))
        .order_by(ChatMessage.created_at).limit(1))


async def list_messages(session: AsyncSession, chat_id: uuid.UUID, limit: int = 50,
                        before: datetime | None = None) -> list[ChatMessage]:
    q = select(ChatMessage).where(ChatMessage.chat_id == chat_id)
    if before:
        q = q.where(ChatMessage.created_at < before)
    rows = (await session.scalars(q.order_by(ChatMessage.created_at.desc()).limit(limit))).all()
    return list(reversed(rows))


async def open_clarifications(session: AsyncSession, chat_id: uuid.UUID) -> list[Clarification]:
    """Unanswered questions, oldest first: answers are matched in the order the questions were asked."""
    return list(await session.scalars(
        select(Clarification).where(Clarification.chat_id == chat_id, Clarification.status == "open")
        .order_by(Clarification.created_at)))


async def open_clarification(session: AsyncSession, chat_id: uuid.UUID) -> Clarification | None:
    questions = await open_clarifications(session, chat_id)
    return questions[0] if questions else None


async def claim_clarification(session: AsyncSession, clarification_id: uuid.UUID, answer: str) -> bool:
    """Atomically close a question with this answer. False if someone else answered it first.

    A conditional UPDATE instead of read-then-write: two messages that both saw the question open
    cannot both answer it."""
    result = await session.execute(
        update(Clarification).where(Clarification.id == clarification_id, Clarification.status == "open")
        .values(status="answered", answer=answer, answered_at=utcnow()).execution_options(synchronize_session=False))
    return result.rowcount == 1


async def get_clarification(session: AsyncSession, chat_id: uuid.UUID, clarification_id: uuid.UUID) -> Clarification | None:
    return await session.scalar(
        select(Clarification).where(Clarification.id == clarification_id, Clarification.chat_id == chat_id))


async def runs_ahead(session: AsyncSession, chat_id: uuid.UUID, run: AgentRun) -> int:
    """Runs of this chat that will be processed before `run`."""
    return await session.scalar(
        select(func.count()).select_from(AgentRun).where(
            AgentRun.chat_id == chat_id, AgentRun.status.in_(ACTIVE_RUN_STATUSES),
            AgentRun.created_at < run.created_at, AgentRun.id != run.id)) or 0


async def active_runs(session: AsyncSession, chat_id: uuid.UUID) -> list[AgentRun]:
    return list(await session.scalars(
        select(AgentRun).where(AgentRun.chat_id == chat_id, AgentRun.status.in_(ACTIVE_RUN_STATUSES))
        .order_by(AgentRun.created_at)))


async def latest_plan(session: AsyncSession, chat_id: uuid.UUID) -> TripPlan | None:
    return await session.scalar(
        select(TripPlan).where(TripPlan.chat_id == chat_id).order_by(TripPlan.updated_at.desc()))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
