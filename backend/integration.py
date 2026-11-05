"""Glue between modules, owned by the host: keeps `trip_map` and `chat` independent of each other.

The map module asks for "a trip source" via its get_source dependency. Here it is wired to the plans that the
agents save in PostgreSQL, restricted to the signed-in Telegram user.
"""
import uuid

from fastapi import Header, HTTPException, Query
from fastapi import Depends
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

import history
import trip_map
from chat import hooks as chat_hooks
from chat import repository as chat_repo
from chat.auth import current_user
from shared.database import get_session
from shared.models import TripPlan
from trip_map.schema import Trip


class LoadedTrip:
    """A trip already read from the database, in the shape trip_map's TripSource expects."""

    def __init__(self, trip: Trip):
        self._trip = trip

    def load(self) -> Trip:
        return self._trip


async def plan_source(plan_id: str | None = Query(None, description="plan uuid, or 'latest' for the active trip"),
                      authorization: str | None = Header(default=None),
                      session: AsyncSession = Depends(get_session)):
    """Without plan_id the map shows the bundled example (works outside Telegram). With it, the plan comes from
    the database and the caller must be signed in (Mini App token)."""
    if plan_id is None:
        return trip_map.router_default_source()
    user = await current_user(authorization=authorization, token=None, session=session)  # 401 without a valid token
    if plan_id == "latest":
        chat = await chat_repo.active_chat(session, user.id)
        plan = await chat_repo.latest_plan(session, chat.id) if chat else None
        if plan is None:
            raise HTTPException(404, "План ещё не собран. Опишите поездку боту.")
    else:
        try:
            plan = await session.get(TripPlan, uuid.UUID(plan_id))
        except ValueError:
            raise HTTPException(422, "plan_id must be a uuid or 'latest'")
        # other people's plans look exactly like missing ones
        if plan is None or await chat_repo.get_user_chat(session, plan.chat_id, user.id) is None:
            raise HTTPException(404, "План не найден")
    try:
        return LoadedTrip(Trip.model_validate(plan.view_json))
    except ValidationError as e:
        raise HTTPException(422, f"План нельзя показать на карте: {e.errors()[0]['msg']}")


def wire(app) -> None:
    app.dependency_overrides[trip_map.get_source] = plan_source
    # a new trip starts with suggestions from the user's past trips (once; wire() may be called by tests repeatedly)
    if history.propose_on_new_trip not in chat_hooks.on_new_trip:
        chat_hooks.on_new_trip.append(history.propose_on_new_trip)
