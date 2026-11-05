import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TelegramAuthIn(BaseModel):
    init_data: str = Field(description="window.Telegram.WebApp.initData, as is")


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    telegram_id: int | None = None  # None for guests (web UI without Telegram)
    is_guest: bool = False
    first_name: str | None = None
    username: str | None = None


class AuthOut(BaseModel):
    token: str
    user: UserOut


class ChatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    status: str
    created_at: datetime


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    # Resending the same id is safe: the message is processed once.
    client_message_id: str | None = Field(default=None, max_length=100)


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    role: str
    text: str
    status: str | None = None
    clarification_id: uuid.UUID | None = None
    plan_id: uuid.UUID | None = None
    plan_revision: int | None = None
    turn: dict | None = None  # the agents' structured answer: why / trade-offs / risks / quick replies
    created_at: datetime


class AnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=4000)
    client_message_id: str | None = Field(default=None, max_length=100)


class SubmittedOut(BaseModel):
    message_id: uuid.UUID
    run_id: uuid.UUID | None  # None when the message was a duplicate
    duplicate: bool
    position: int = 0  # runs ahead in this chat's queue


class PlanOut(BaseModel):
    plan_id: uuid.UUID
    revision: int
    status: str
    view: dict
    updated_at: datetime
