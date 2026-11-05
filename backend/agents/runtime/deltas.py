"""LLM deltas -> typed operations. This is where a model's loose output is clamped to the closed op set."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from ..domain.models import Source, TripState
from shared.templates import apply_items

from ..domain.ops import (AddConstraint, SetInherited, Op, PinOffer, PriorityIn, RelaxConstraint, RemoveConstraint,
                          ExcludeOffer, SetBudget, SetDates, SetDestinationUnresolved, SetOrigin, SetParty,
                          SetPriorities, SetRoute, SetScope, StopIn)
from ..domain.scoring import PRIORITY_ALIAS, PRIORITY_KEYS
from .schemas import BudgetOut, ConstraintsOut, PlannerOut

KNOWN_KINDS = {"breakfast", "free_cancellation", "pets_allowed", "kid_friendly", "min_stars", "min_rating", "bed_type", "view",
               "max_travel_hours", "max_transfers", "exclude_modes", "only_modes", "avoid_overnight", "free_text"}
NUMERIC_KINDS = {"min_stars", "min_rating", "max_travel_hours", "max_transfers"}


def _src(message_id: str | None, quote: str | None, origin: str = "user") -> Source:
    return Source(message_id=message_id, quote=quote, origin=origin)  # type: ignore[arg-type]


def planner_ops(d: PlannerOut, state: TripState, message_id: str | None) -> list[Op]:
    src = _src(message_id, d.quote)
    ops: list[Op] = []
    stops = [s for s in (d.stops or []) if s.name and s.name.strip()]
    if d.scope:
        ops.append(SetScope(scope=d.scope, source=src))
    elif state.scope is None and (stops or d.destination_vague):
        ops.append(SetScope(scope="full", source=_src(message_id, d.quote, "default")))
    stop_names = {x.name.strip().lower() for x in stops} | {(state.places[x.place].name or "").lower() for x in state.route if x.place in state.places}
    if d.origin and d.origin.strip() and d.origin.strip().lower() not in stop_names:  # "from X to X" is the model echoing the destination
        ops.append(SetOrigin(name=d.origin.strip(), source=src))
    if stops:
        nights = d.nights_total if (len(stops) == 1 and d.nights_total and not stops[0].nights) else None
        ops.append(SetRoute(stops=[StopIn(name=s.name.strip(), nights=nights or s.nights) for s in stops],
                            round_trip=d.round_trip, source=src))
    elif d.destination_vague and not state.route:
        ops.append(SetDestinationUnresolved(text=d.destination_vague, source=src))
    elif d.round_trip is not None and state.route:
        ops.append(SetRoute(stops=[StopIn(name=state.places[s.place].name or s.place, nights=s.nights) for s in state.route],
                            round_trip=d.round_trip, source=src))

    date_to = d.date_to
    base_from = d.date_from or state.date_from
    if d.nights_total and not date_to and base_from and not d.shift_days:
        date_to = base_from + timedelta(days=d.nights_total)
    if d.nights_total and not base_from and not stops and len(state.route) == 1:
        st = state.route[0]
        ops.append(SetRoute(stops=[StopIn(name=state.places[st.place].name or st.place, nights=d.nights_total)], source=src))
    if d.date_from or date_to or d.shift_days or d.month_hint:
        ops.append(SetDates(date_from=d.date_from, date_to=date_to, shift_days=d.shift_days,
                            month_hint=d.month_hint, source=src))
    for nid in d.pin_nodes:
        if nid in state.nodes and state.nodes[nid].selected_offer_ref:
            ops.append(PinOffer(node_id=nid, source=src))
    for nid in d.replace_nodes:
        if nid in state.nodes and state.nodes[nid].type != "transfer":
            ops.append(ExcludeOffer(node_id=nid, source=src))
    return ops


def _clean_value(kind: str, value):
    if kind in NUMERIC_KINDS:
        try:
            return float(value) if kind != "max_transfers" else int(float(value))
        except (TypeError, ValueError):
            return None
    if kind in ("exclude_modes", "only_modes"):
        return value if isinstance(value, list) else [value]
    return value


def _priorities(items) -> list[PriorityIn]:
    out = []
    for p in items:
        key = PRIORITY_ALIAS.get(p.key, p.key)
        if key in PRIORITY_KEYS:
            out.append(PriorityIn(key=key, weight=max(0.0, min(float(p.weight), 5.0))))
    return out


def constraints_ops(d: ConstraintsOut, state: TripState, message_id: str | None) -> list[Op]:
    src = _src(message_id, d.quote)
    ops: list[Op] = []
    if any(v is not None for v in (d.adults, d.add_adults, d.children_ages, d.add_children_ages, d.children_unknown, d.pets)):
        ops.append(SetParty(adults=d.adults, add_adults=d.add_adults, children_ages=d.children_ages,
                            add_children_ages=d.add_children_ages, children_unknown=d.children_unknown,
                            pets=d.pets, source=src))
    if d.pets and not any(c.kind.strip().lower() == "pets_allowed" for c in d.add) \
            and not any(c.kind == "pets_allowed" for c in state.constraints):
        # "с собакой" is a condition of the stay, whatever the model did with it
        ops.append(AddConstraint(kind="pets_allowed", value=True, strength="hard", label="с питомцем", source=src))
    for c in d.add:
        kind = c.kind.strip().lower()
        if kind not in KNOWN_KINDS:
            kind, c.value, c.strength, c.label = "free_text", c.label or c.kind, "soft", c.label or c.kind
        value = _clean_value(kind, c.value)
        if value is None:
            continue
        ops.append(AddConstraint(kind=kind, value=value, strength=c.strength, label=c.label,
                                 source=_src(message_id, c.quote or d.quote)))
    ops += [RemoveConstraint(kind=k, source=src) for k in d.remove]
    ops += [RelaxConstraint(kind=k, source=src) for k in d.relax]
    if d.priorities:
        ops.append(SetPriorities(items=_priorities(d.priorities), replace=d.replace_priorities, source=src))
    return ops


def budget_ops(d: BudgetOut, state: TripState, message_id: str | None, current_scoped_total: Decimal | None) -> list[Op]:
    src = _src(message_id, d.quote)
    ops: list[Op] = []
    if d.amount is not None and d.per_person and state.party.total:
        d = d.model_copy(update={"amount": d.amount * state.party.total})  # "30 000 на каждого" for two is 60 000 in total
    if d.amount is not None:
        ops.append(SetBudget(amount=d.amount, scope=d.scope or (state.budget.scope if state.budget.amount else
                                                                  ("stay" if state.scope == "stay" else "trip")), source=src))
    elif d.scope:
        ops.append(SetBudget(scope=d.scope, source=src))
    if d.qualitative and d.amount is None:
        ops.append(SetBudget(qualitative=d.qualitative, source=src))
        ops.append(SetPriorities(items=[PriorityIn(key="price", weight=1.5)], source=src))
    if d.direction == "cheaper":
        ops.append(SetPriorities(items=[PriorityIn(key="price", weight=2.0)], source=src))
        if current_scoped_total and current_scoped_total > 0:
            target = (current_scoped_total * Decimal("0.85") / 100).quantize(Decimal("1")) * 100
            ops.append(SetBudget(amount=target, source=_src(message_id, d.quote, "derived")))
    elif d.direction == "cheapest":
        ops.append(SetPriorities(items=[PriorityIn(key="price", weight=4.0)], source=src))
    return ops


def inherited_ops(state: TripState, accepted: list[tuple[dict, str, str]]) -> list[Op]:
    """Accepted items from past trips -> operations. Uses the history module's own merge rules (shared.templates):
    what the user said in THIS trip always wins, hotel hints never become constraints."""
    fresh = [t for t in accepted if t[2] not in state.inherited_applied]
    if not fresh:
        return []
    config = {"constraints": [{"kind": c.kind} for c in state.constraints], "priorities": [{"key": p.key} for p in state.priorities]}
    out = apply_items(config, fresh)
    ops: list[Op] = []
    for c in out["constraints"][len(config["constraints"]):]:
        ops.append(AddConstraint(kind=c["kind"], value=c["value"], strength=c["strength"], label=c.get("label"),
                                 source=Source(origin="history", quote=c["source"]["quote"])))
    new_prio = out["priorities"][len(config["priorities"]):]
    if new_prio:
        ops.append(SetPriorities(items=[PriorityIn(key=p["key"], weight=p["weight"]) for p in new_prio],
                                 source=Source(origin="history", quote=new_prio[0]["source"]["quote"])))
    ops.append(SetInherited(applied_ids=[t[2] for t in fresh], hints=out["history_hints"]))
    return ops
