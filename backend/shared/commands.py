"""Commands the chat module sends to the agents module.

They travel through the `agent_runs` table (the worker picks up `queued` rows), carry only ids,
and never copy the history or the plan: agents load both from PostgreSQL.
"""
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter


class ProcessUserMessage(BaseModel):
    type: Literal["ProcessUserMessage"] = "ProcessUserMessage"
    command_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    chat_id: uuid.UUID
    message_id: uuid.UUID
    plan_id: uuid.UUID | None = None


class ProcessClarificationAnswer(BaseModel):
    type: Literal["ProcessClarificationAnswer"] = "ProcessClarificationAnswer"
    command_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    chat_id: uuid.UUID
    message_id: uuid.UUID
    clarification_id: uuid.UUID
    plan_id: uuid.UUID | None = None


class ApplyUserAction(BaseModel):
    """A click in the UI that changes the plan without a chat message: choose a destination, pick an alternative,
    pin / unpin. It goes through the same queue so it never races with a running search."""
    type: Literal["ApplyUserAction"] = "ApplyUserAction"
    command_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    chat_id: uuid.UUID
    action: dict  # {"type": "choose_destination" | "select_offer" | "unpin", ...}
    plan_id: uuid.UUID | None = None


AgentCommand = Annotated[ProcessUserMessage | ProcessClarificationAnswer | ApplyUserAction, Field(discriminator="type")]
agent_command_adapter: TypeAdapter[AgentCommand] = TypeAdapter(AgentCommand)
