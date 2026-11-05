"""Applying typed operations to a TripState: permissions, conflicts, invariants, dependency tracking.

`apply_ops` works on a deep copy and never raises for a bad operation: it records it as rejected
with a reason, so one invalid op from an LLM does not lose the valid ones.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

from . import guard
from .geo import place_from_name
from .cartfp import cart_fingerprint
from .graph import mark_stale, rebuild_graph, search_sig
from .models import (Cart, Constraint, Node, Place, Priority, Question, RouteStop, TripState)
from .ops import PERMISSIONS, Op, touches

STRUCTURE_OPS = {"set_scope", "set_origin", "set_route", "set_dates", "choose_destination",
                 "set_destination_unresolved"}


class Rejected(Exception):
    pass


@dataclass
class ApplyResult:
    state: TripState
    applied: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    changed_node_ids: list[str] = field(default_factory=list)


def _record(op: Op, status: str, reason: str | None = None) -> dict[str, Any]:
    dump = op.model_dump(mode="json", exclude={"offers"})
    if hasattr(op, "offers"):
        dump["offer_ids"] = [o.id for o in op.offers]
    return {"op": dump, "status": status, "reason": reason}


# ------------------------------------------------------------------ handlers

def _ensure_place(s: TripState, name: str) -> str:
    pid, place = place_from_name(name)
    if pid not in s.places:
        s.places[pid] = place
    return pid


def _remember(s: TripState, path: str, op: Any) -> None:
    src = getattr(op, "source", None)
    if src:
        s.facts[path] = src


def _set_scope(s, op):
    s.scope = op.scope
    _remember(s, "scope", op)


def _set_origin(s, op):
    s.origin = _ensure_place(s, op.name)
    _remember(s, "origin", op)


def _set_route(s, op):
    if not op.stops:
        raise Rejected("empty route")
    prev = {st.place: st for st in s.route}
    stops = []
    for st in op.stops:
        pid = _ensure_place(s, st.name)
        old = prev.get(pid)
        if st.nights is None and old is not None:
            stops.append(RouteStop(place=pid, nights=old.nights, nights_origin=old.nights_origin))
        else:
            stops.append(RouteStop(place=pid, nights=st.nights, nights_origin=st.nights_origin))
    s.route = stops
    if op.round_trip is not None:
        s.round_trip = op.round_trip
    s.kind = "city" if len(stops) == 1 else "multi_city"
    s.destination_unresolved = False
    _remember(s, "route", op)


def _reset_derived_nights(s: TripState) -> None:
    for st in s.route:
        if st.nights_origin in ("agent", "derived"):
            st.nights = None


def _fit_nights(s: TripState) -> None:
    """The dates the user gave win over nights remembered from an earlier wording: [1 Nov, 4 Nov] + "to 6 Nov"
    must not leave the hotel at 1-4 Nov. A single stop takes the whole span; with several, the last one absorbs the
    difference (and is flagged as the agent's proposal); if that is impossible the agent redistributes everything."""
    if not (s.date_from and s.date_to and s.route):
        return
    total = (s.date_to - s.date_from).days
    known = sum(st.nights or 0 for st in s.route)
    has_free = any(not st.nights for st in s.route)
    if known == total or (has_free and known < total):
        return
    if len(s.route) == 1:
        s.route[0].nights, s.route[0].nights_origin = None, "derived"
        return
    rest = total - sum(st.nights or 0 for st in s.route[:-1])
    if rest > 0:
        s.route[-1].nights, s.route[-1].nights_origin = rest, "agent"
    else:
        for st in s.route:
            st.nights, st.nights_origin = None, "agent"


def _set_dates(s, op):
    old_len = (s.date_to - s.date_from).days if s.date_from and s.date_to else None
    if op.shift_days:
        if not (s.date_from or s.date_to):
            raise Rejected("no dates to shift")
        delta = timedelta(days=op.shift_days)
        s.date_from = s.date_from + delta if s.date_from else None
        s.date_to = s.date_to + delta if s.date_to else None
    if op.date_from:
        s.date_from = op.date_from
        if op.date_to is None and old_len is not None:
            s.date_to = op.date_from + timedelta(days=old_len)
    if op.date_to:
        s.date_to = op.date_to
    if s.date_from and s.date_to and (s.date_to < s.date_from or (s.date_to == s.date_from and s.scope != "transport")):
        raise Rejected("date_to must be after date_from")  # a same-day round trip is possible only without a stay
    if op.month_hint:
        s.month_hint = op.month_hint
    if op.date_from or op.shift_days:
        s.month_hint = None
    _reset_derived_nights(s)
    _fit_nights(s)
    _remember(s, "dates", op)


def _set_unresolved(s, op):
    s.destination_unresolved = True
    s.destination_text = op.text
    s.route, s.nodes, s.sequence, s.kind = [], {}, [], None
    _remember(s, "destination", op)


def _choose_destination(s, op):
    name = op.name or op.key
    c = next((c for c in s.destination_candidates if c.key == op.key), None)
    pid = _ensure_place(s, c.name if c else name)
    s.route = [RouteStop(place=pid, nights=None, nights_origin="agent")]
    s.kind = "city"
    s.destination_unresolved = False
    _reset_derived_nights(s)


def _set_title(s, op):
    s.title = op.title


def _set_party(s, op):
    p = s.party
    if op.adults is not None:
        p.adults = op.adults  # an absolute headcount wins: "adults=4" plus "add 3" must not become 7
    elif op.add_adults:
        p.adults = (p.adults or 0) + op.add_adults
    if op.children_ages is not None:
        p.children_ages = list(op.children_ages)
        if op.children_unknown is None:
            p.children_unknown = 0
    if op.add_children_ages:
        p.children_ages = p.children_ages + list(op.add_children_ages)
    if op.children_unknown is not None:
        p.children_unknown = op.children_unknown
    if op.pets is not None:
        p.pets = list(op.pets)
    if p.adults is None and (p.children_ages or p.children_unknown):
        pass  # adults stay unknown: the gap check will ask
    _remember(s, "party", op)


def _add_constraint(s, op):
    s.constraints = [c for c in s.constraints if c.kind != op.kind]
    s.constraints.append(Constraint(id=op.kind, kind=op.kind, value=op.value, strength=op.strength,
                                    label=op.label, source=op.source))


def _remove_constraint(s, op):
    if not any(c.kind == op.kind for c in s.constraints):
        raise Rejected(f"no constraint '{op.kind}'")
    s.constraints = [c for c in s.constraints if c.kind != op.kind]


def _relax_constraint(s, op):
    hit = [c for c in s.constraints if c.kind == op.kind]
    if not hit:
        raise Rejected(f"no constraint '{op.kind}'")
    for c in hit:
        c.strength = "soft"


def _set_priorities(s, op):
    items = [Priority(key=i.key, weight=i.weight, source=op.source) for i in op.items]
    if op.replace:
        s.priorities = items
    else:
        keep = {p.key: p for p in s.priorities}
        for i in items:
            keep[i.key] = i
        s.priorities = list(keep.values())


def _set_inherited(s, op):
    s.inherited_applied = list(dict.fromkeys([*s.inherited_applied, *op.applied_ids]))
    have = {(h.get("polarity"), (h.get("provider_hotel_id") or h.get("name") or "").lower()) for h in s.history_hints}
    for h in op.hints:
        key = (h.get("polarity"), (h.get("provider_hotel_id") or h.get("name") or "").lower())
        if key not in have:
            s.history_hints.append(h)
            have.add(key)


def _set_budget(s, op):
    b = s.budget
    if op.amount is not None:
        if op.amount <= 0:
            raise Rejected("budget must be positive")
        b.amount = op.amount
        b.qualitative = None
    if op.scope:
        b.scope = op.scope
    if op.qualitative is not None:
        b.qualitative = op.qualitative
    b.source = op.source
    _remember(s, "budget", op)


def _node(s: TripState, node_id: str) -> Node:
    n = s.nodes.get(node_id)
    if n is None:
        raise Rejected(f"unknown node '{node_id}'")
    return n


def _attach_options(s, op):
    n = _node(s, op.node_id)
    if n.type == "transfer":
        raise Rejected("transfers have no options")
    for o in op.offers:
        s.offers[o.id] = o
    n.option_ids = [o.id for o in op.offers]
    n.coverage = op.coverage
    n.search_sig = search_sig(s, n)
    n.scores = {}
    kept = n.selected_offer_ref if (n.pinned and n.selected_offer_ref in n.option_ids) else None
    if n.pinned and kept is None:
        n.pinned = False
    n.selected_offer_ref = kept
    n.status = ("selected" if kept else "options") if op.offers else "not_found"


def _set_ranking(s, op):
    n = _node(s, op.node_id)
    unknown = set(op.scores) - set(n.option_ids)
    if unknown:
        raise Rejected(f"scores for unknown offers: {sorted(unknown)}")
    n.scores = op.scores
    if op.selected is not None and op.selected not in n.option_ids:
        raise Rejected("selected offer is not among the node's options")
    if not n.pinned or op.selected is not None:
        n.selected_offer_ref = op.selected
    _sync_selection(s, n)


def _sync_selection(s: TripState, n: Node) -> None:
    if n.selected_offer_ref:
        n.status = "selected"
        o = s.offers.get(n.selected_offer_ref)
        if o and o.transport:
            n.mode = o.transport.mode
    elif n.option_ids:
        n.status = "options"


def _select_offer(s, op):
    n = _node(s, op.node_id)
    if op.offer_id not in n.option_ids:
        raise Rejected("offer is not among the node's options")
    n.selected_offer_ref = op.offer_id
    if op.pin:
        n.pinned = True
    _sync_selection(s, n)


def _pin(s, op):
    n = _node(s, op.node_id)
    if not n.selected_offer_ref:
        raise Rejected("nothing selected to pin")
    n.pinned = True


def _unpin(s, op):
    _node(s, op.node_id).pinned = False


def _exclude(s, op):
    n = _node(s, op.node_id)
    if n.selected_offer_ref:
        n.excluded_offers = list({*n.excluded_offers, n.selected_offer_ref})
    n.selected_offer_ref, n.pinned, n.status = None, False, "stale"


def _set_guest_feedback(s, op):
    o = s.offers.get(op.offer_id)
    if o is None or o.hotel is None:
        raise Rejected(f"no hotel offer '{op.offer_id}'")
    # defence in depth: whatever the caller passed is cleaned again before it can reach a model or a user
    o.hotel.pros = [q for q in (guard.clean_snippet(x) for x in op.pros) if q][:2]
    o.hotel.cons = [q for q in (guard.clean_snippet(x) for x in op.cons) if q][:2]
    o.hotel.reviews_used, o.hotel.feedback_loaded = op.reviews_used, True


def _set_node_dates(s, op):
    n = _node(s, op.node_id)
    if op.check_in:
        n.check_in = op.check_in
    if op.check_out:
        n.check_out = op.check_out
    if op.departure_date:
        n.departure_date = op.departure_date
    if n.check_in and n.check_out and n.check_out <= n.check_in:
        raise Rejected("check_out must be after check_in")


def _set_places(s, op):
    for pid, fields in op.items.items():
        merged = {**(s.places[pid].model_dump() if pid in s.places else {}), **fields}
        s.places[pid] = Place.model_validate(merged)


def _set_transfers(s, op):
    for nid in [i for i, n in s.nodes.items() if n.type == "transfer"]:
        del s.nodes[nid]
        s.sequence.remove(nid)
    for it in op.items:
        after = it.get("after")
        if after not in s.nodes:
            raise Rejected(f"transfer anchored to unknown node '{after}'")
        n = Node(id=it["id"], type="transfer", from_=it["from_"], to=it["to"], mode="drive",
                 departure_date=it.get("departure_date"), status="selected",
                 attrs={k: it[k] for k in ("duration_min", "distance_km", "estimated", "direction") if k in it})
        s.nodes[n.id] = n
        idx = s.sequence.index(after)
        if it.get("direction") == "before":
            s.sequence.insert(idx, n.id)
        else:
            s.sequence.insert(idx + 1, n.id)


def _ask(s, op):
    open_fields = {q.field for q in s.questions if q.status == "open"}
    n = len(s.questions)
    for q in op.questions:
        if q.field in open_fields:
            continue
        s.questions.append(Question(**{**q.model_dump(), "id": q.id or f"q{n + 1}"}))
        n += 1


def _resolve(s, op):
    q = next((q for q in s.questions if q.id == op.id), None)
    if q is None:
        raise Rejected(f"unknown question '{op.id}'")
    q.status, q.answer = "answered", op.answer


def _set_candidates(s, op):
    s.destination_candidates = op.candidates


def _set_checks(s, op):
    s.checks = op.checks


def _set_notes(s, op):
    s.data_notes = [d for d in s.data_notes if not d.scope.startswith(op.scope_prefix)] + list(op.notes)


def _set_status(s, op):
    s.status = op.status


def _set_cart(s, op):
    s.cart = Cart.model_validate(op.cart)


def _accept_checks(s, op):
    s.accepted_checks = sorted(set(s.accepted_checks) | set(op.keys))


HANDLERS: dict[str, Callable[[TripState, Any], None]] = {
    "set_scope": _set_scope, "set_origin": _set_origin, "set_route": _set_route, "set_dates": _set_dates,
    "set_destination_unresolved": _set_unresolved, "choose_destination": _choose_destination,
    "set_title": _set_title, "set_party": _set_party, "add_constraint": _add_constraint,
    "remove_constraint": _remove_constraint, "relax_constraint": _relax_constraint,
    "set_priorities": _set_priorities, "set_inherited": _set_inherited, "set_budget": _set_budget, "attach_options": _attach_options,
    "set_ranking": _set_ranking, "select_offer": _select_offer, "pin_offer": _pin, "unpin_offer": _unpin,
    "exclude_offer": _exclude, "set_transfers": _set_transfers, "set_node_dates": _set_node_dates, "set_guest_feedback": _set_guest_feedback, "set_places": _set_places, "ask": _ask,
    "resolve_question": _resolve, "set_destination_candidates": _set_candidates, "set_checks": _set_checks,
    "set_data_notes": _set_notes, "set_status": _set_status, "set_cart": _set_cart, "accept_checks": _accept_checks,
}


def _node_dump(s: TripState) -> dict[str, str]:
    return {k: v.model_dump_json() for k, v in s.nodes.items()}


def apply_ops(state: TripState, actor: str, ops: list[Op], *, base_revision: int | None = None,
              newer_touched: set[str] | None = None) -> ApplyResult:
    """Apply `ops` from `actor` to a copy of `state`.

    `base_revision` + `newer_touched`: the revision the actor worked from and the state areas
    changed since then. Operations touching those areas are rejected as stale; the rest apply.
    """
    s = state.model_copy(deep=True)
    res = ApplyResult(state=s)
    allowed = PERMISSIONS.get(actor, set())
    before = _node_dump(s)
    structure_changed = False
    for op in ops:
        name = op.op
        if name not in allowed:
            res.rejected.append(_record(op, "rejected", f"actor '{actor}' may not use '{name}'"))
            continue
        if base_revision is not None and base_revision < s.revision and newer_touched:
            clash = touches(op) & newer_touched
            if clash:
                res.rejected.append(_record(op, "rejected", f"stale: {sorted(clash)} changed since revision {base_revision}"))
                continue
        snapshot = s.model_copy(deep=True)
        try:
            HANDLERS[name](s, op)
        except Rejected as e:
            s.__dict__.update(snapshot.__dict__)  # roll back a half-applied op
            res.rejected.append(_record(op, "rejected", str(e)))
            continue
        res.applied.append(_record(op, "applied"))
        structure_changed |= name in STRUCTURE_OPS
    if structure_changed:
        rebuild_graph(s)
    mark_stale(s)
    if s.cart.status in ("draft", "checked") and s.cart.fingerprint and cart_fingerprint(s, s.cart.items) != s.cart.fingerprint:
        s.cart = Cart()  # the plan moved on under the cart: it must be built (and checked) again
        if s.status == "in_cart":
            s.status = "partial"
    after = _node_dump(s)
    res.changed_node_ids = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    if res.applied:
        s.revision += 1
        # keep only offers some node still references
        live = {oid for n in s.nodes.values() for oid in n.option_ids}
        s.offers = {k: v for k, v in s.offers.items() if k in live}
    return res
