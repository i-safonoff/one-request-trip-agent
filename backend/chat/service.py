"""Chat use cases shared by the Telegram bot and the HTTP API.

Every use case commits. Anything that needs the agents goes through `agent_runs` + commands
(shared.commands); the chat module never imports the agents module.
"""
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.commands import AgentCommand, ProcessClarificationAnswer, ProcessUserMessage
from shared.events import EventType, RequestQueued, emit_event
from shared.models import AgentRun, Chat, ChatMessage, Clarification, TripPlan, User

from . import repository as repo
from .hooks import run_new_trip_hooks
from .notifier import notifier

MAX_TEXT_LENGTH = 4000
# An identical message inside this window, while the first is still queued, is a double send.
DUPLICATE_WINDOW_SECONDS = 60


class EmptyMessage(ValueError):
    pass


@dataclass
class Submitted:
    message: ChatMessage
    run: AgentRun | None  # None for a duplicate (already handled)
    duplicate: bool = False
    answered: Clarification | None = None  # set when the text answered an open question
    position: int = 0


@dataclass
class ChatStatus:
    open_question: Clarification | None
    active_runs: int
    has_plan: bool


class ChatService:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ---- users and chats ----
    async def register(self, telegram_id: int, username: str | None, first_name: str | None,
                       language_code: str | None, telegram_chat_id: int | None) -> tuple[User, Chat]:
        """Create or update the user and make sure they have an active chat."""
        user = await repo.upsert_user(self.session, telegram_id, username, first_name, language_code)
        chat = await repo.active_chat(self.session, user.id)
        if chat is None:
            chat = Chat(user_id=user.id, telegram_chat_id=telegram_chat_id)
            self.session.add(chat)
        elif telegram_chat_id is not None:
            chat.telegram_chat_id = telegram_chat_id
        await self.session.commit()
        return user, chat

    async def active_for(self, user: User) -> Chat:
        """The user's active chat, created if missing. Works for Telegram users and for guests (no telegram_id)."""
        if user.telegram_id is not None:
            return (await self.register(user.telegram_id, user.username, user.first_name, user.language_code,
                                        user.telegram_id))[1]
        chat = await repo.active_chat(self.session, user.id)
        if chat is None:
            chat = Chat(user_id=user.id)
            self.session.add(chat)
            await self.session.commit()
        return chat

    async def new_trip(self, user: User, telegram_chat_id: int | None) -> Chat:
        """Archive the current chat and start an empty one (the old plan stays readable by id)."""
        await repo.archive_active_chats(self.session, user.id)
        chat = Chat(user_id=user.id, telegram_chat_id=telegram_chat_id)
        self.session.add(chat)
        await self.session.commit()
        await run_new_trip_hooks(self.session, user, chat)  # e.g. suggestions from past trips
        return chat

    # ---- messages ----
    async def submit_text(self, chat: Chat, text: str, external_id: str | None = None,
                          question: Clarification | None = None) -> Submitted | None:
        """Handle a user's text and turn it into at most one agent run.

        The text answers `question` (button press) or else the oldest open question; with no open question it
        starts a request. Returns None only when an explicit `question` is no longer open. It never creates a second run for the same input:
          * the same message redelivered (same external_id) is ignored;
          * the same text sent twice while the first is still waiting for the agents is ignored;
          * a question takes exactly one answer: if two messages race for it, the second answers the next one.
        """
        text = (text or "").strip()
        if not text:
            raise EmptyMessage()
        text = text[:MAX_TEXT_LENGTH]

        # One message of a chat at a time (row lock on PostgreSQL), so queue order and question matching are stable.
        await self.session.execute(select(Chat.id).where(Chat.id == chat.id).with_for_update())

        if external_id and (seen := await repo.find_message(self.session, chat.id, external_id)):
            await self.session.commit()
            return Submitted(message=seen, run=None, duplicate=True)
        message, created = await repo.add_message(
            self.session, chat.id, "user", text, status="queued", external_id=external_id)
        if not created:  # lost a race against the same redelivery
            await self.session.commit()
            return Submitted(message=message, run=None, duplicate=True)

        answered = None
        if question is not None:  # an explicit target (button): it must still be open
            if await repo.claim_clarification(self.session, question.id, text):
                answered = question
            else:
                await self._discard(message)
                return None
        else:
            for candidate in await repo.open_clarifications(self.session, chat.id):
                if await repo.claim_clarification(self.session, candidate.id, text):
                    answered = candidate
                    break  # a candidate that was just answered elsewhere is skipped: try the next question
        if answered is None:
            twin = await repo.recent_identical_queued(self.session, chat.id, text, DUPLICATE_WINDOW_SECONDS, message.id)
            if twin is not None:
                await self._discard(message)
                return Submitted(message=twin, run=None, duplicate=True)

        plan = await repo.latest_plan(self.session, chat.id)
        plan_id = plan.id if plan else None
        if answered:
            message.clarification_id = answered.id
            command: AgentCommand = ProcessClarificationAnswer(
                chat_id=chat.id, message_id=message.id, clarification_id=answered.id, plan_id=plan_id)
        else:
            command = ProcessUserMessage(chat_id=chat.id, message_id=message.id, plan_id=plan_id)

        run = AgentRun(chat_id=chat.id, command=command.model_dump(mode="json"), status="queued")
        self.session.add(run)
        await self.session.flush()
        position = await repo.runs_ahead(self.session, chat.id, run)
        await emit_event(self.session, chat.id, EventType.REQUEST_QUEUED,
                         RequestQueued(message_id=message.id, position=position), run_id=run.id)
        await self.session.commit()
        notifier.poke()
        return Submitted(message=message, run=run, answered=answered, position=position)

    async def _discard(self, message: ChatMessage) -> None:
        """Drop a message that turned out not to need processing (and release the chat lock)."""
        await self.session.delete(message)
        await self.session.commit()

    async def answer_clarification(self, chat: Chat, clarification_id: uuid.UUID, answer: str,
                                   external_id: str | None = None) -> Submitted | None:
        """Answer a specific question (button press). None if it is unknown or already answered."""
        question = await repo.get_clarification(self.session, chat.id, clarification_id)
        if question is None or question.status != "open":
            return None
        return await self.submit_text(chat, answer, external_id, question=question)

    async def status(self, chat: Chat) -> ChatStatus:
        return ChatStatus(
            open_question=await repo.open_clarification(self.session, chat.id),
            active_runs=len(await repo.active_runs(self.session, chat.id)),
            has_plan=await repo.latest_plan(self.session, chat.id) is not None,
        )

    async def plan(self, chat: Chat) -> TripPlan | None:
        return await repo.latest_plan(self.session, chat.id)
