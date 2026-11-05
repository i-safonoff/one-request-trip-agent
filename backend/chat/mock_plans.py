"""Mock trip plans for demos (DEV_AGENT mode). Not part of the product: real plans come from the agents module.

The plans are the trip_map example configs. One place saves them, so the dev agent and /demo behave the same.
"""
import json
import re
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from shared.events import AssistantMessage, EventType, emit_event
from shared.models import Chat, ChatMessage, TripPlan, TripPlanVersion

from .repository import latest_plan, utcnow

EXAMPLES = Path(__file__).resolve().parent.parent / "trip_map" / "examples"
SCENARIOS = {"moscow_spb": "trip_moscow_spb.json", "japan": "trip.json"}
ALIASES = {"moscow": "moscow_spb", "msk": "moscow_spb", "spb": "moscow_spb", "москва": "moscow_spb",
           "питер": "moscow_spb", "tokyo": "japan", "japan": "japan", "япония": "japan", "токио": "japan"}


def load_mock(scenario: str) -> dict:
    return json.loads((EXAMPLES / SCENARIOS[scenario]).read_text("utf-8"))


def parse_scenario(arg: str | None) -> str | None:
    """`/demo` argument -> scenario name; no argument means Moscow + St Petersburg. None if unknown."""
    if not arg:
        return "moscow_spb"
    return ALIASES.get(arg.strip().lower())


def pick_mock(user_texts: list[str]) -> dict:
    """Moscow + St Petersburg if the conversation mentions them, otherwise the Japan trip."""
    scenario = "moscow_spb" if re.search(r"москв|питер|петербург|спб", " ".join(user_texts), re.I) else "japan"
    return load_mock(scenario)


async def save_mock_plan(session: AsyncSession, chat_id, mock: dict) -> TripPlan:
    """Create the chat's plan, or bump its revision, and keep a snapshot of every revision."""
    plan = await latest_plan(session, chat_id)
    if plan is None:
        plan = TripPlan(chat_id=chat_id, revision=1)
        session.add(plan)
    else:
        plan.revision += 1
    plan.status, plan.view_json, plan.state_json, plan.updated_at = "ready", mock, mock, utcnow()
    await session.flush()
    session.add(TripPlanVersion(plan_id=plan.id, revision=plan.revision, state_json=mock, view_json=mock))
    return plan


async def add_demo_plan(session: AsyncSession, chat: Chat, scenario: str) -> TripPlan:
    """Give the chat a ready mock plan and announce it like an agent would (assistant message + map button)."""
    mock = load_mock(scenario)
    plan = await save_mock_plan(session, chat.id, mock)
    reply = ChatMessage(chat_id=chat.id, role="assistant", plan_id=plan.id, plan_revision=plan.revision,
                        text=f"Демо-план «{mock['title']}»: отели и переезды. Откройте карту.")
    session.add(reply)
    await session.flush()
    await emit_event(session, chat.id, EventType.ASSISTANT_MESSAGE, AssistantMessage(
        message_id=reply.id, text=reply.text, plan_id=plan.id, revision=plan.revision))
    await session.commit()
    return plan
