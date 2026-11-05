"""Tables shared by the chat and agents modules.

Ownership: `chat` writes users / chats / chat_messages / clarifications; `agents` writes
trip_plans / trip_plan_versions and consumes agent_runs; both append to chat_events.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# JSONB on PostgreSQL, plain JSON elsewhere
JSONType = JSON().with_variant(JSONB(), "postgresql")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # None for guests (the web UI at http://localhost:8080 works without Telegram); unique among Telegram users
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    is_guest: Mapped[bool] = mapped_column(Boolean, default=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Chat(Base):
    """One conversation = one trip. A user has at most one active chat; /new archives it."""
    __tablename__ = "chats"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)  # where the bot writes; None for API-only chats
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | archived
    # Telegram delivery state: last event pushed to the bot chat, and the editable "progress" message
    delivered_seq: Mapped[int] = mapped_column(Integer, default=0)
    progress_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (UniqueConstraint("chat_id", "external_id"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    text: Mapped[str] = mapped_column(Text)
    # user messages: queued -> processing -> done | failed (assistant messages: None)
    status: Mapped[str | None] = mapped_column(String(16))
    # dedupe key for redelivered input: "tg:<message_id>" or "api:<client_message_id>"
    external_id: Mapped[str | None] = mapped_column(String(128))
    clarification_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # assistant messages from the agents: structured answer (why / trade-offs / risks / quick replies), see agents.domain.turn
    turn: Mapped[dict | None] = mapped_column(JSONType)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    plan_revision: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Clarification(Base):
    __tablename__ = "clarifications"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"), index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    field: Mapped[str | None] = mapped_column(String(128))  # which part of the plan the answer fills
    question: Mapped[str] = mapped_column(Text)
    options: Mapped[list | None] = mapped_column(JSONType)  # optional quick-reply buttons
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | answered
    answer: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRun(Base):
    """The queue between chat and agents: chat inserts `queued` rows, the agents worker consumes them."""
    __tablename__ = "agent_runs"
    # at most one running run per chat: messages of one trip are processed strictly in order
    __table_args__ = (Index("agent_runs_one_running", "chat_id", unique=True,
                            postgresql_where=text("status = 'running'"), sqlite_where=text("status = 'running'")),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"), index=True)
    command: Mapped[dict] = mapped_column(JSONType)  # shared.commands.AgentCommand as JSON
    # queued | running | waiting_clarification | done | failed
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # a stale heartbeat requeues the run
    checkpoint: Mapped[dict | None] = mapped_column(JSONType)  # lets a restarted worker resume without redoing a step
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatEvent(Base):
    """Append-only event log. `sequence` is the cursor for SSE resume and Telegram delivery."""
    __tablename__ = "chat_events"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, default=uuid.uuid4, unique=True)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"), index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class TripPlan(Base):
    """Written by the agents module; the chat module only reads it."""
    __tablename__ = "trip_plans"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    state_json: Mapped[dict] = mapped_column(JSONType, default=dict)  # internal state of the agents
    view_json: Mapped[dict] = mapped_column(JSONType, default=dict)   # what the map / cards render
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class TripPlanVersion(Base):
    __tablename__ = "trip_plan_versions"
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trip_plans.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str | None] = mapped_column(String(120))  # "Найдены варианты", "Корзина создана", ...
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    state_json: Mapped[dict] = mapped_column(JSONType, default=dict)
    view_json: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class InheritedItem(Base):
    """A piece of a past trip offered to (and decided on by) the user for a NEW trip. The history module writes it,
    the agents module reads `accepted` rows and applies them (shared.templates.apply_items)."""
    __tablename__ = "inherited_items"
    __table_args__ = (UniqueConstraint("chat_id", "item_key"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"), index=True)  # the new trip
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    item_key: Mapped[str] = mapped_column(String(160))  # e.g. "constraint:breakfast": one proposal per key per trip
    item: Mapped[dict] = mapped_column(JSONType)  # shared.templates item (ConstraintItem / PriorityItem / HotelHint)
    reason: Mapped[str] = mapped_column(Text)  # human-readable "why", shown to the user
    confidence: Mapped[float] = mapped_column(default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="proposed")  # proposed | accepted | dismissed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ------------------------------------------------------------------ agents module: journal, offers, cart, orders

class PlanOperation(Base):
    """Journal of typed operations applied to a plan: who changed what, based on which quote."""
    __tablename__ = "plan_operations"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"), index=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trip_plans.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    actor: Mapped[str] = mapped_column(String(24))
    op: Mapped[dict] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(String(16))  # applied | rejected
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OfferRow(Base):
    """Normalised offers a plan has used, with the source and whether the data is live or demo."""
    __tablename__ = "offers"
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"), primary_key=True)
    offer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(24))
    data_mode: Mapped[str] = mapped_column(String(8))  # live | mock
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONType)


class Cart(Base):
    __tablename__ = "carts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"), index=True)
    plan_revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | checked | paid
    items: Mapped[list] = mapped_column(JSONType)
    total: Mapped[float | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    price_changes: Mapped[list] = mapped_column(JSONType, default=list)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Order(Base):
    """A SIMULATED order: no payment was taken and nothing was booked with a supplier."""
    __tablename__ = "orders"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    number: Mapped[str] = mapped_column(String(16), unique=True)
    cart_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("carts.id"))  # one order per cart (uq_orders_cart_id below)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"), index=True)
    guest: Mapped[dict] = mapped_column(JSONType)
    total: Mapped[float] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(16), default="paid_mock")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (UniqueConstraint("cart_id", name="uq_orders_cart_id"),)
