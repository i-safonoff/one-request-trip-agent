"""HTTP endpoints of the agents module, on the chat module's sessions and tables:

* guest sign-in (the web UI at http://localhost:8080 works without Telegram);
* chat status (is the agent busy, which question is open);
* UI actions that change the plan without a chat message (choose a destination, pick an alternative, pin/unpin);
  they go through the same `agent_runs` queue, so they never race with a running search.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from chat import repository as repo
from chat.auth import current_user, issue_token
from chat.router import _owned_chat
from chat.schemas import AuthOut, UserOut
from shared.commands import ApplyUserAction
from shared.config import get_settings
from shared.database import get_session
from shared.models import AgentRun, ChatEvent as ChatEventRow, TripPlan, User

router = APIRouter(prefix="/api/v1", tags=["agents"])


@router.post("/auth/guest", response_model=AuthOut)
async def auth_guest(session: AsyncSession = Depends(get_session)):
    """A session without Telegram: for the browser at http://localhost:8080."""
    user = User(telegram_id=None, is_guest=True, first_name="Гость")
    session.add(user)
    await session.commit()
    try:
        token = issue_token(user.id, get_settings())
    except RuntimeError:
        raise HTTPException(503, "Не задан SESSION_SECRET (или токен бота): подписать сессию нечем")
    return AuthOut(token=token, user=UserOut.model_validate(user))


@router.get("/chats/{chat_id}/status")
async def chat_status(chat_id: uuid.UUID, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    chat = await _owned_chat(chat_id, user, session)
    q = await repo.open_clarification(session, chat.id)
    last = await session.scalar(select(func.max(ChatEventRow.sequence)).where(ChatEventRow.chat_id == chat.id))
    return {"busy": len(await repo.active_runs(session, chat.id)) > 0, "last_event": last or 0,
            "open_question": {"id": str(q.id), "question": q.question, "options": q.options or []} if q else None}


async def _enqueue_action(session: AsyncSession, chat_id: uuid.UUID, action: dict) -> dict:
    plan = await repo.latest_plan(session, chat_id)
    command = ApplyUserAction(chat_id=chat_id, action=action, plan_id=plan.id if plan else None)
    run = AgentRun(chat_id=chat_id, command=command.model_dump(mode="json"), status="queued")
    session.add(run)
    await session.commit()
    return {"run_id": str(run.id)}


@router.post("/chats/{chat_id}/destinations/{key}/choose", status_code=202)
async def choose_destination(chat_id: uuid.UUID, key: str, user: User = Depends(current_user),
                             session: AsyncSession = Depends(get_session)):
    chat = await _owned_chat(chat_id, user, session)
    return await _enqueue_action(session, chat.id, {"type": "choose_destination", "key": key})


@router.post("/chats/{chat_id}/accept-dates", status_code=202)
async def accept_dates(chat_id: uuid.UUID, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    """The user agrees to the dates the stays got after a late arrival: the plan can then be paid."""
    chat = await _owned_chat(chat_id, user, session)
    return await _enqueue_action(session, chat.id, {"type": "accept_dates"})


class SelectIn(BaseModel):
    offer_id: str
    pin: bool = True


async def _plan_chat(plan_id: uuid.UUID, user: User, session: AsyncSession) -> uuid.UUID:
    plan = await session.get(TripPlan, plan_id)
    if plan is None or await repo.get_user_chat(session, plan.chat_id, user.id) is None:
        raise HTTPException(404, "План не найден")  # other people's plans look exactly like missing ones
    return plan.chat_id


@router.post("/plans/{plan_id}/nodes/{node_id}/select", status_code=202)
async def select_offer(plan_id: uuid.UUID, node_id: str, body: SelectIn, user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    chat_id = await _plan_chat(plan_id, user, session)
    return await _enqueue_action(session, chat_id, {"type": "select_offer", "node_id": node_id,
                                                    "offer_id": body.offer_id, "pin": body.pin})


@router.post("/plans/{plan_id}/nodes/{node_id}/unpin", status_code=202)
async def unpin(plan_id: uuid.UUID, node_id: str, user: User = Depends(current_user),
                session: AsyncSession = Depends(get_session)):
    chat_id = await _plan_chat(plan_id, user, session)
    return await _enqueue_action(session, chat_id, {"type": "unpin", "node_id": node_id})
