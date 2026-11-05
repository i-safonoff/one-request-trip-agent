"""PlanStore on the shared tables (SQLAlchemy): trip_plans + trip_plan_versions, the operation journal, offers, events.

One TripPlan row per chat; every checkpoint of a run bumps its revision, keeps an immutable version and publishes
`plan.updated` in the same transaction (so the event is visible only after the data it describes).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

from chat import repository as chat_repo
from shared.database import Database
from shared.events import PAYLOADS, EventType, PlanChanged, emit_event
from shared.models import AgentRun, Cart as CartRow, InheritedItem, OfferRow, Order, PlanOperation, TripPlan, TripPlanVersion

from ..domain.models import TripState
from ..domain.view import build_view


def _uuid(v: str | uuid.UUID | None) -> uuid.UUID | None:
    return v if isinstance(v, uuid.UUID) else (uuid.UUID(v) if v else None)


class DbPlanStore:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ plan
    async def load(self, chat_id: str) -> TripState:
        async with self.db.sessionmaker() as s:
            plan = await chat_repo.latest_plan(s, _uuid(chat_id))
        if plan and plan.state_json:
            try:
                return TripState.model_validate(plan.state_json)
            except ValueError:  # e.g. a plan written by the demo agent: start from an empty state
                pass
        return TripState(trip_id=str(chat_id))

    async def plan_id(self, chat_id: str) -> uuid.UUID | None:
        async with self.db.sessionmaker() as s:
            plan = await chat_repo.latest_plan(s, _uuid(chat_id))
            return plan.id if plan else None

    async def save(self, chat_id, run_id, state: TripState, records: list[dict], changed: list[str], label: str) -> TripState:
        cid, rid = _uuid(chat_id), _uuid(run_id)
        state = state.model_copy(deep=True)
        async with self.db.sessionmaker() as s:
            plan = await s.scalar(select(TripPlan).where(TripPlan.chat_id == cid).order_by(TripPlan.updated_at.desc())
                                  .limit(1).with_for_update())
            rev = (plan.revision + 1) if plan else 1
            state.revision = rev
            state_json = state.model_dump(mode="json", by_alias=True)
            view = build_view(state)
            if plan is None:
                plan = TripPlan(chat_id=cid, revision=rev, status=state.status, state_json=state_json, view_json=view)
                s.add(plan)
                await s.flush()
            else:
                plan.revision, plan.status, plan.state_json, plan.view_json = rev, state.status, state_json, view
                plan.updated_at = datetime.now(timezone.utc)
            s.add(TripPlanVersion(plan_id=plan.id, revision=rev, label=label, run_id=rid, state_json=state_json, view_json=view))
            for r in records:
                s.add(PlanOperation(chat_id=cid, plan_id=plan.id, revision=rev, run_id=rid, actor=r.get("actor", "?"),
                                    op=r["op"], status=r["status"], reason=r.get("reason")))
            for o in state.offers.values():
                await s.merge(OfferRow(chat_id=cid, offer_id=o.id, source=o.source, data_mode=o.data_mode,
                                       retrieved_at=o.retrieved_at, payload=o.model_dump(mode="json")))
            await emit_event(s, cid, EventType.PLAN_UPDATED,
                             PlanChanged(plan_id=plan.id, revision=rev, changed_node_ids=changed), rid)
            await s.commit()
        return state

    async def cancel_order(self, chat_id: str) -> None:
        """Simulated cancellation after the purchase: the paid cart and its order stop counting (rows are kept for the history)."""
        cid = _uuid(chat_id)
        async with self.db.sessionmaker() as s:
            await s.execute(update(CartRow).where(CartRow.chat_id == cid, CartRow.status == "paid").values(status="cancelled"))
            await s.execute(update(Order).where(Order.chat_id == cid, Order.status == "paid_mock").values(status="cancelled_mock"))
            await s.commit()

    # ------------------------------------------------------------------ events
    async def emit(self, chat_id, run_id, type: str, payload: dict[str, Any]) -> None:
        """Publish an event; `payload` is validated against the shared model of that type."""
        t = EventType(type)
        cid = _uuid(chat_id)
        if t in (EventType.PLAN_READY, EventType.PLAN_UPDATED):
            payload = {"changed_node_ids": [], **payload, "plan_id": payload.get("plan_id") or await self.plan_id(chat_id)}
        model = PAYLOADS[t].model_validate(payload)
        async with self.db.sessionmaker() as s:
            await emit_event(s, cid, t, model, _uuid(run_id))
            await s.commit()

    async def inherited_items(self, chat_id: str) -> list[tuple[dict, str, str]]:
        """Items the user accepted from past trips for this trip: [(item, reason, id)]."""
        async with self.db.sessionmaker() as s:
            rows = await s.scalars(select(InheritedItem).where(InheritedItem.chat_id == _uuid(chat_id), InheritedItem.status == "accepted")
                                   .order_by(InheritedItem.created_at))
            return [(r.item, r.reason, str(r.id)) for r in rows]

    # ------------------------------------------------------------------ run checkpoints
    async def get_checkpoint(self, run_id: str) -> dict:
        async with self.db.sessionmaker() as s:
            run = await s.get(AgentRun, _uuid(run_id))
            return dict(run.checkpoint or {}) if run else {}

    async def set_checkpoint(self, run_id: str, data: dict) -> None:
        async with self.db.sessionmaker() as s:
            await s.execute(update(AgentRun).where(AgentRun.id == _uuid(run_id)).values(checkpoint=data))
            await s.commit()
