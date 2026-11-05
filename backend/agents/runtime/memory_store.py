"""In-memory PlanStore: unit tests and the no-database dev mode."""
from __future__ import annotations

from ..domain.models import TripState


class MemoryPlanStore:
    def __init__(self):
        self.states: dict[str, TripState] = {}
        self.versions: list[tuple[str, int, str]] = []
        self.events: list[tuple[str, str, dict]] = []
        self.journal: list[dict] = []
        self.checkpoints: dict[str, dict] = {}
        self.inherited: dict[str, list] = {}  # tests: accepted history items per chat

    async def inherited_items(self, chat_id: str) -> list:
        return self.inherited.get(chat_id, [])

    async def cancel_order(self, chat_id: str) -> None:
        self.events.append((chat_id, "order.cancelled", {}))

    async def get_checkpoint(self, run_id: str) -> dict:
        return self.checkpoints.get(run_id, {})

    async def set_checkpoint(self, run_id: str, data: dict) -> None:
        self.checkpoints[run_id] = data

    async def load(self, chat_id: str) -> TripState:
        return self.states.get(chat_id, TripState(trip_id=chat_id)).model_copy(deep=True)

    async def save(self, chat_id, run_id, state, records, changed, label) -> TripState:
        prev = self.states.get(chat_id)
        state = state.model_copy(deep=True)
        state.revision = (prev.revision if prev else 0) + 1
        self.states[chat_id] = state
        self.versions.append((chat_id, state.revision, label))
        self.journal += [{"chat_id": chat_id, "revision": state.revision, **r} for r in records]
        self.events.append((chat_id, "plan.updated", {"revision": state.revision, "changed_node_ids": changed, "label": label}))
        return state.model_copy(deep=True)

    async def emit(self, chat_id, run_id, type, payload) -> None:
        self.events.append((chat_id, type, payload))
