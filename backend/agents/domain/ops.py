"""Typed operations on TripState: the only way an agent may change the plan.

LLM agents never write paths or raw JSON patches. They return *deltas* (see agents/schemas.py);
code maps those onto these operations, Pydantic validates each one, and `PERMISSIONS` says which
actor may use which operation. `touches()` names the parts of the state an operation changes,
so a stale base revision only conflicts with operations that really overlap.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field

from .models import (Check, DataNote, DestinationCandidate, Offer, OptionScore,
                     Question, Source, Strength)


class _Op(BaseModel):
    source: Source | None = None


class StopIn(BaseModel):
    name: str
    nights: int | None = Field(default=None, ge=1, le=60)
    nights_origin: Literal["user", "agent", "derived"] = "user"


class PriorityIn(BaseModel):
    key: str
    weight: float = Field(default=1.0, ge=0, le=5)


# ---- planner (main agent)
class SetScope(_Op):
    op: Literal["set_scope"] = "set_scope"
    scope: Literal["stay", "transport", "full"]


class SetOrigin(_Op):
    op: Literal["set_origin"] = "set_origin"
    name: str


class SetRoute(_Op):
    op: Literal["set_route"] = "set_route"
    stops: list[StopIn]
    round_trip: bool | None = None


class SetDates(_Op):
    op: Literal["set_dates"] = "set_dates"
    date_from: date | None = None
    date_to: date | None = None
    shift_days: int | None = None
    month_hint: str | None = None


class SetDestinationUnresolved(_Op):
    op: Literal["set_destination_unresolved"] = "set_destination_unresolved"
    text: str | None = None


class ChooseDestination(_Op):
    op: Literal["choose_destination"] = "choose_destination"
    key: str
    name: str | None = None


class SetTitle(_Op):
    op: Literal["set_title"] = "set_title"
    title: str


# ---- constraints agent
class SetParty(_Op):
    op: Literal["set_party"] = "set_party"
    adults: int | None = Field(default=None, ge=1, le=20)
    children_ages: list[int] | None = None
    children_unknown: int | None = Field(default=None, ge=0, le=10)
    add_adults: int | None = Field(default=None, ge=1, le=20)
    add_children_ages: list[int] | None = None
    pets: list[str] | None = None


class AddConstraint(_Op):
    op: Literal["add_constraint"] = "add_constraint"
    kind: str
    value: Any = True
    strength: Strength = "hard"
    label: str | None = None


class RemoveConstraint(_Op):
    op: Literal["remove_constraint"] = "remove_constraint"
    kind: str


class RelaxConstraint(_Op):
    op: Literal["relax_constraint"] = "relax_constraint"
    kind: str


class SetInherited(_Op):
    """Mark history items as applied and keep hotel hints (inherited from past trips)."""
    op: Literal["set_inherited"] = "set_inherited"
    applied_ids: list[str]
    hints: list[dict[str, Any]] = []


class SetPriorities(_Op):
    op: Literal["set_priorities"] = "set_priorities"
    items: list[PriorityIn]
    replace: bool = False


# ---- budget agent
class SetBudget(_Op):
    op: Literal["set_budget"] = "set_budget"
    amount: Decimal | None = None
    scope: Literal["trip", "stay", "transport"] | None = None
    qualitative: str | None = None


# ---- hotels / transport / system (code)
class AttachOptions(_Op):
    op: Literal["attach_options"] = "attach_options"
    node_id: str
    offers: list[Offer]
    coverage: Literal["full", "partial"] = "partial"


class SetRanking(_Op):
    op: Literal["set_ranking"] = "set_ranking"
    node_id: str
    scores: dict[str, OptionScore]
    selected: str | None = None


class SelectOffer(_Op):
    op: Literal["select_offer"] = "select_offer"
    node_id: str
    offer_id: str
    pin: bool = False


class PinOffer(_Op):
    op: Literal["pin_offer"] = "pin_offer"
    node_id: str


class UnpinOffer(_Op):
    op: Literal["unpin_offer"] = "unpin_offer"
    node_id: str


class ExcludeOffer(_Op):
    """'Replace this hotel': drop the current choice and make the node searchable again."""
    op: Literal["exclude_offer"] = "exclude_offer"
    node_id: str


class SetTransfers(_Op):
    op: Literal["set_transfers"] = "set_transfers"
    items: list[dict[str, Any]]  # {id, from_place, to_place, departure_date, duration_min, distance_km, after}


class SetNodeDates(_Op):
    """Dates of ONE node, set by code after a transport is chosen (e.g. check-in moved to the arrival day)."""
    op: Literal["set_node_dates"] = "set_node_dates"
    node_id: str
    check_in: date | None = None
    check_out: date | None = None
    departure_date: date | None = None


class SetGuestFeedback(_Op):
    """Quotes from guest reviews for a chosen hotel (fetched after the choice, one call per hotel)."""
    op: Literal["set_guest_feedback"] = "set_guest_feedback"
    offer_id: str
    pros: list[str] = []
    cons: list[str] = []
    reviews_used: int = 0


class SetPlaces(_Op):
    op: Literal["set_places"] = "set_places"
    items: dict[str, dict[str, Any]]  # place id -> fields to merge (coords, tz, parent, kind)


# ---- dialog
class Ask(_Op):
    op: Literal["ask"] = "ask"
    questions: list[Question]


class ResolveQuestion(_Op):
    op: Literal["resolve_question"] = "resolve_question"
    id: str
    answer: str | None = None


class SetDestinationCandidates(_Op):
    op: Literal["set_destination_candidates"] = "set_destination_candidates"
    candidates: list[DestinationCandidate]


# ---- verifier / notes / status
class SetChecks(_Op):
    op: Literal["set_checks"] = "set_checks"
    checks: list[Check]


class SetDataNotes(_Op):
    op: Literal["set_data_notes"] = "set_data_notes"
    scope_prefix: str
    notes: list[DataNote]


class SetStatus(_Op):
    op: Literal["set_status"] = "set_status"
    status: Literal["draft", "searching", "needs_input", "choosing_destination", "ready", "partial",
                    "in_cart", "booked"]


class AcceptChecks(_Op):
    op: Literal["accept_checks"] = "accept_checks"
    keys: list[str]


class SetCart(_Op):
    op: Literal["set_cart"] = "set_cart"
    cart: dict[str, Any]


Op = Annotated[Union[
    SetScope, SetOrigin, SetRoute, SetDates, SetDestinationUnresolved, ChooseDestination, SetTitle,
    SetParty, AddConstraint, RemoveConstraint, RelaxConstraint, SetPriorities, SetInherited, SetBudget,
    AttachOptions, SetRanking, SelectOffer, PinOffer, UnpinOffer, ExcludeOffer, SetTransfers, SetNodeDates, SetGuestFeedback, SetPlaces,
    Ask, ResolveQuestion, SetDestinationCandidates, SetChecks, SetDataNotes, SetStatus, SetCart, AcceptChecks,
], Field(discriminator="op")]


PERMISSIONS: dict[str, set[str]] = {
    "planner": {"set_scope", "set_origin", "set_route", "set_dates", "set_destination_unresolved",
                "set_title", "ask", "resolve_question", "pin_offer", "unpin_offer", "exclude_offer"},
    "constraints": {"set_party", "add_constraint", "remove_constraint", "relax_constraint", "set_priorities", "set_inherited"},
    "budget": {"set_budget", "set_priorities", "select_offer"},
    "hotels": {"attach_options", "set_ranking", "set_places", "set_guest_feedback"},
    "transport": {"attach_options", "set_ranking", "set_transfers", "set_places"},
    "destinations": {"set_destination_candidates"},
    "verifier": {"set_checks"},
    "orchestrator": {"set_node_dates", "set_status", "set_data_notes", "set_places", "set_ranking", "select_offer",
                     "set_transfers", "ask", "set_cart", "set_destination_candidates"},
    "user": {"select_offer", "pin_offer", "unpin_offer", "exclude_offer", "choose_destination",
             "resolve_question", "set_cart", "accept_checks"},
}


def touches(op: _Op) -> set[str]:
    """Which parts of the state the operation writes. Used to detect conflicts with newer revisions."""
    name = op.op  # type: ignore[attr-defined]
    node = getattr(op, "node_id", None)
    table = {
        "set_scope": {"route"}, "set_origin": {"route"}, "set_route": {"route"},
        "set_dates": {"dates", "route"}, "set_destination_unresolved": {"route"},
        "choose_destination": {"route"}, "set_title": {"title"},
        "set_party": {"party"}, "add_constraint": {"constraints"}, "remove_constraint": {"constraints"},
        "relax_constraint": {"constraints"}, "set_priorities": {"priorities"}, "set_inherited": {"constraints"}, "set_budget": {"budget"},
        "set_checks": {"checks"}, "set_status": {"status"}, "ask": {"questions"},
        "resolve_question": {"questions"}, "set_destination_candidates": {"candidates"},
        "set_data_notes": {"notes"}, "set_cart": {"cart"}, "set_places": {"places"},
        "set_transfers": {"transfers"}, "set_guest_feedback": {"offers"},
    }
    if name in table:
        return table[name]
    return {f"node:{node}"} if node else set()
