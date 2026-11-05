"""What is still missing before the next useful step. One batch of questions, never a form.

Rule from the plan: ask only what blocks the next action. A missing optional preference
(bed type, stars) is never a gap; a missing city, date or headcount is.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import TripState


@dataclass
class Gap:
    field: str
    text: str
    options: list[str] = field(default_factory=list)
    blocking: bool = True


def blocking_gaps(state: TripState) -> list[Gap]:
    gaps: list[Gap] = []
    scope = state.scope or "full"
    needs_transport = scope in ("full", "transport")

    if not state.route and not state.destination_unresolved:
        gaps.append(Gap("destination", "Куда хотите поехать? Можно назвать город, страну или описать настроение: «к морю», «в горы».",
                        blocking=True))
    if state.destination_unresolved:
        # Inspiration needs only a time frame (for the climate). Where from and how many travellers are asked AFTER
        # the user picks a destination: the track's own example ("тёплое море в ноябре, недорого, с ребёнком")
        # must get destinations first, not a questionnaire.
        if not (state.date_from or state.month_hint):
            gaps.append(Gap("dates", "Когда планируете поездку: примерные даты или месяц, и на сколько дней?"))
        return gaps
    if needs_transport and state.origin is None:
        # a whole trip without a known origin still gets its stays now; tickets are added once the user answers
        gaps.append(Gap("origin", "Откуда выезжаете? Добавлю билеты туда и обратно. Если билеты не нужны, так и напишите." if scope == "full" else "Откуда выезжаете?",
                        blocking=scope == "transport"))
    if state.route and not (state.date_from and (state.date_to or all(s.nights for s in state.route))):
        gaps.append(Gap("dates", "На какие даты? Достаточно дня заезда и числа ночей."))
    if state.party.adults is None:
        gaps.append(Gap("party", "Сколько вас едет? Если с детьми, укажите их возраст."))
    elif state.party.children_unknown:
        gaps.append(Gap("children_ages", "Сколько лет ребёнку (детям)? От возраста зависит цена и размещение."))
    return gaps


def questions_text(gaps: list[Gap]) -> str:
    """Several short questions in one message."""
    if len(gaps) == 1:
        return gaps[0].text
    return "Чтобы продолжить, уточните:\n" + "\n".join(f"{i}. {g.text}" for i, g in enumerate(gaps, 1))
