"""Tables owned by the history module. The cross-module table (shared.models.InheritedItem) lives in shared."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from shared.models import Base, JSONType


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TripFeedback(Base):
    """What a traveller said about a trip or one stay of it. One row per (trip, node); `node_id` is '' for the trip."""
    __tablename__ = "trip_feedback"
    __table_args__ = (UniqueConstraint("chat_id", "node_id"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id"), index=True)
    node_id: Mapped[str] = mapped_column(String(128), default="")
    sentiment: Mapped[int] = mapped_column(Integer)  # -1 disliked, 0 neutral, 1 liked
    aspects: Mapped[dict] = mapped_column(JSONType, default=dict)  # {"location": 1, "price": -1}
    comment: Mapped[str | None] = mapped_column(Text)  # stored, shown back to the user, never fed to a model as is
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PreferenceItem(Base):
    """The user's profile: items derived from ALL their feedback. Rebuilt from feedback, never edited by hand, so it
    can't drift from its evidence. `muted` is the user's "stop suggesting this"."""
    __tablename__ = "preference_items"
    __table_args__ = (UniqueConstraint("user_id", "item_key"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    item_key: Mapped[str] = mapped_column(String(160))
    item: Mapped[dict] = mapped_column(JSONType)
    confidence: Mapped[float] = mapped_column(Float)
    observed_count: Mapped[int] = mapped_column(Integer, default=1)  # distinct trips behind it
    evidence: Mapped[list] = mapped_column(JSONType, default=list)
    muted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class TripTemplate(Base):
    """A saved fragment of a trip config (history.skeleton.TemplateFragment). Per user only: templates are never shared
    between users. `parent_id` is the single allowed `extends` (depth 1)."""
    __tablename__ = "trip_templates"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    source_chat_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("chats.id"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("trip_templates.id"))
    fragment: Mapped[dict] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
