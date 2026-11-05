"""Chat events: what the agents module (and the chat module) publish, and what clients see.

Agents call `emit_event` inside the same transaction that saves their change, so an event only becomes
visible after the data it describes is committed.
"""
import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ChatEvent as ChatEventRow


class EventType(StrEnum):
    REQUEST_QUEUED = "request.queued"
    AGENT_PROGRESS = "agent.progress"
    CLARIFICATION_REQUIRED = "clarification.required"
    PLAN_UPDATED = "plan.updated"
    PLAN_READY = "plan.ready"
    ASSISTANT_MESSAGE = "assistant.message"
    REQUEST_FAILED = "request.failed"
    SUGGESTIONS_PROPOSED = "suggestions.proposed"
    DESTINATIONS_READY = "destinations.ready"  # the agents proposed destinations (the plan carries the cards)
    ORDER_PAID = "order.paid"  # simulated payment done


class RequestQueued(BaseModel):
    message_id: uuid.UUID
    position: int = 0  # runs ahead of this one in the chat's queue


class AgentProgress(BaseModel):
    stage: str | None = None  # machine-readable, e.g. "searching_hotels"
    text: str  # shown to the user: "Ищу проживание в Токио…"


class ClarificationRequired(BaseModel):
    clarification_id: uuid.UUID
    question: str
    options: list[str] | None = None
    field: str | None = None


class PlanChanged(BaseModel):  # plan.updated and plan.ready
    plan_id: uuid.UUID
    revision: int
    changed_node_ids: list[str] = []


class AssistantMessage(BaseModel):
    message_id: uuid.UUID
    text: str
    plan_id: uuid.UUID | None = None
    revision: int | None = None


class RequestFailed(BaseModel):
    reason: str  # human-readable, safe to show


class DestinationsReady(BaseModel):
    count: int


class OrderPaid(BaseModel):
    order_number: str
    total: str
    currency: str


class SuggestionView(BaseModel):
    suggestion_id: uuid.UUID
    text: str  # "Завтрак включён"


class SuggestionsProposed(BaseModel):
    """Things from past trips offered for the new one; the user accepts or dismisses each."""
    intro: str
    items: list[SuggestionView]


PAYLOADS: dict[EventType, type[BaseModel]] = {
    EventType.REQUEST_QUEUED: RequestQueued,
    EventType.AGENT_PROGRESS: AgentProgress,
    EventType.CLARIFICATION_REQUIRED: ClarificationRequired,
    EventType.PLAN_UPDATED: PlanChanged,
    EventType.PLAN_READY: PlanChanged,
    EventType.ASSISTANT_MESSAGE: AssistantMessage,
    EventType.REQUEST_FAILED: RequestFailed,
    EventType.SUGGESTIONS_PROPOSED: SuggestionsProposed,
    EventType.DESTINATIONS_READY: DestinationsReady,
    EventType.ORDER_PAID: OrderPaid,
}


class ChatEvent(BaseModel):
    """Event as clients receive it (SSE / API)."""
    event_id: uuid.UUID
    sequence: int
    type: EventType
    chat_id: uuid.UUID
    run_id: uuid.UUID | None = None
    created_at: datetime
    payload: dict = Field(default_factory=dict)

    @classmethod
    def from_row(cls, row: ChatEventRow) -> "ChatEvent":
        return cls(event_id=row.event_id, sequence=row.sequence, type=EventType(row.type), chat_id=row.chat_id,
                   run_id=row.run_id, created_at=row.created_at, payload=row.payload or {})


async def emit_event(session: AsyncSession, chat_id: uuid.UUID, type: EventType, payload: BaseModel,
                     run_id: uuid.UUID | None = None) -> ChatEventRow:
    """Append an event. The payload model must match the type; the caller commits."""
    expected = PAYLOADS[type]
    if not isinstance(payload, expected):
        raise TypeError(f"{type.value} needs {expected.__name__}, got {type(payload).__name__}")
    row = ChatEventRow(chat_id=chat_id, run_id=run_id, type=type.value, payload=payload.model_dump(mode="json"))
    session.add(row)
    await session.flush()  # assigns `sequence`
    return row


async def events_after(session: AsyncSession, chat_id: uuid.UUID, after: int = 0, limit: int = 200) -> list[ChatEvent]:
    rows = await session.scalars(
        select(ChatEventRow).where(ChatEventRow.chat_id == chat_id, ChatEventRow.sequence > after)
        .order_by(ChatEventRow.sequence).limit(limit))
    return [ChatEvent.from_row(r) for r in rows]
