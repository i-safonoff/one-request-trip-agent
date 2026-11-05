"""Prompt inputs: the compact state brief and the conversation history the agents see."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from ..domain import guard
from ..domain.models import TripState

PROMPTS = Path(__file__).parent / "prompts"


def prompt(name: str) -> str:
    """A system prompt, always preceded by the security preamble (see domain/guard.py)."""
    return guard.PREAMBLE + "\n\n" + (PROMPTS / f"{name}.md").read_text("utf-8")


def all_prompts() -> list[str]:
    return [prompt(p.stem) for p in sorted(PROMPTS.glob("*.md"))]


def state_brief(state: TripState) -> dict:
    """Compact view of the plan: enough for the model to refer to nodes by id, nothing it could abuse."""
    def pname(pid):
        return state.places[pid].name if pid in state.places else pid

    nodes = []
    for nid in state.sequence:
        n = state.nodes[nid]
        if n.type == "transfer":
            continue
        o = state.offers.get(n.selected_offer_ref or "")
        label = f"{pname(n.from_)} → {pname(n.to)}" if n.type == "transport" else f"проживание: {pname(n.place)}"
        nodes.append({"id": nid, "what": label, "dates": str(n.check_in or n.departure_date or ""),
                      "selected": o.title() if o else None, "pinned": n.pinned})
    return {
        "scope": state.scope, "origin": pname(state.origin) if state.origin else None,
        "route": [{"city": pname(s.place), "nights": s.nights} for s in state.route],
        "dates": {"from": str(state.date_from) if state.date_from else None, "to": str(state.date_to) if state.date_to else None,
                  "month_hint": state.month_hint},
        "party": state.party.model_dump(), "budget": state.budget.model_dump(mode="json"),
        "constraints": [{"kind": c.kind, "value": c.value, "strength": c.strength} for c in state.constraints],
        "priorities": [{"key": p.key, "weight": p.weight} for p in state.priorities],
        "destination_vague": state.destination_text if state.destination_unresolved else None,
        "nodes": nodes,
        "open_questions": [{"field": q.field, "text": q.text} for q in state.open_questions()],
    }


_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _fri_on_or_after(d: date) -> date:
    return d + timedelta(days=(4 - d.weekday()) % 7)


def calendar_anchors(today: date) -> dict:
    """Ready dates for relative phrases ("завтра", "на выходные", "в конце месяца"): models are bad at calendar arithmetic."""
    def nxt(weekday: int) -> date:  # the nearest such weekday strictly after today
        return today + timedelta(days=(weekday - today.weekday() - 1) % 7 + 1)
    last_day = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return {
        "today_weekday": _WEEKDAYS[today.weekday()],
        "tomorrow": (today + timedelta(days=1)).isoformat(),
        "day_after_tomorrow": (today + timedelta(days=2)).isoformat(),
        "nearest_friday": nxt(4).isoformat(), "nearest_saturday": nxt(5).isoformat(), "nearest_sunday": nxt(6).isoformat(),
        "next_monday": nxt(0).isoformat(),
        "end_of_current_month": last_day.isoformat(),
        "in_one_week": (today + timedelta(days=7)).isoformat(),
        "weekend_after_one_week": {"friday": _fri_on_or_after(today + timedelta(days=7)).isoformat(),
                                   "sunday": (_fri_on_or_after(today + timedelta(days=7)) + timedelta(days=2)).isoformat()},
        "next_weekend": {"friday": _fri_on_or_after(today).isoformat(),
                         "sunday": (_fri_on_or_after(today) + timedelta(days=2)).isoformat()},
    }


def user_prompt(message: str, state: TripState, history: list[dict], today: date) -> str:
    return json.dumps({
        "today": today.isoformat(),
        "calendar": calendar_anchors(today),
        "message": message,
        "state": state_brief(state),
        "recent_messages": history[-6:],
    }, ensure_ascii=False)
