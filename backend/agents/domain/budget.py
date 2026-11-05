"""Pricing of the current selection and budget-aware choice of the best set of offers.

All money maths is Decimal and happens here, never in an LLM.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import Node, TripState
from .scoring import candidates


def in_budget_scope(state: TripState, node: Node) -> bool:
    scope = state.budget.scope
    if node.type == "transfer":
        return False
    return scope == "trip" or (scope == "stay" and node.type == "stay") or (scope == "transport" and node.type == "transport")


def _priced_nodes(state: TripState) -> list[Node]:
    """Nodes whose price is part of the trip total (transfers are not priced)."""
    scope = state.scope or "full"
    return [n for n in (state.nodes[i] for i in state.sequence)
            if n.type != "transfer" and (scope == "full" or (scope == "stay" and n.type == "stay")
                                         or (scope == "transport" and n.type == "transport"))]


def scope_needs_origin(state: TripState) -> bool:
    return (state.scope or "full") in ("full", "transport") and state.origin is None and bool(state.route)


def compute_pricing(state: TripState) -> dict:
    nodes = _priced_nodes(state)
    selected = [(n, state.offers[n.selected_offer_ref]) for n in nodes
                if n.status != "stale" and n.selected_offer_ref and n.selected_offer_ref in state.offers]
    cur = next((o.price.currency for _, o in selected), state.budget.currency)
    subtotal = sum((o.price.amount for _, o in selected), Decimal(0))
    # a node waiting for a new search keeps its old offer for comparison, but that offer is not a price of this plan
    missing = [n.id for n in nodes if n.status == "stale" or not (n.selected_offer_ref and n.selected_offer_ref in state.offers)]
    needs_origin = scope_needs_origin(state)
    complete = bool(nodes) and not missing and not needs_origin  # tickets to and from the trip are part of a full plan
    has_transfers = any(n.type == "transfer" for n in state.nodes.values())
    b = state.budget
    scoped = sum((o.price.amount for n, o in selected if in_budget_scope(state, n)), Decimal(0))
    scoped_missing = [m for m in missing if in_budget_scope(state, state.nodes[m])]
    within = None
    if b.amount is not None and selected:
        within = scoped <= b.amount if not scoped_missing else (None if scoped <= b.amount else False)
    return {
        "currency": cur,
        "known_subtotal": f"{subtotal:.2f}",
        "total": f"{subtotal:.2f}" if complete else None,
        "is_complete": complete,
        "missing": missing,
        "needs_origin": needs_origin,
        "excluded": ["transfers"] if has_transfers else [],
        "budget_scoped_total": f"{scoped:.2f}",
        "within_budget": within,
    }


@dataclass
class Choice:
    selected: dict[str, str | None]
    scoped_total: Decimal
    min_scoped_total: Decimal
    within: bool | None


def choose_set(state: TripState) -> Choice:
    """Pick one offer per node maximising total score subject to the budget (Pareto DP).

    Pinned nodes are fixed. Nodes outside the budget scope take their best-scored offer.
    If the budget cannot be met, the cheapest combination is chosen and `within` is False.
    """
    nodes = [state.nodes[i] for i in state.sequence if state.nodes[i].type != "transfer"]
    fixed: dict[str, str | None] = {}
    free: list[tuple[Node, list[str]]] = []
    for n in nodes:
        cands = candidates(state, n)
        if n.pinned and n.selected_offer_ref:
            fixed[n.id] = n.selected_offer_ref
        elif not cands:
            fixed[n.id] = None
        elif not in_budget_scope(state, n) or state.budget.amount is None:
            fixed[n.id] = max(cands, key=lambda i: n.scores[i].score)
        else:
            free.append((n, cands))

    def price(oid: str | None) -> Decimal:
        return state.offers[oid].price.amount if oid else Decimal(0)

    base = sum((price(fixed[n.id]) for n in nodes if n.id in fixed and in_budget_scope(state, n)), Decimal(0))
    min_total = base + sum((min(price(i) for i in c) for _, c in free), Decimal(0))
    if not free:
        return Choice(fixed, base, base, None if state.budget.amount is None else base <= state.budget.amount)

    budget = state.budget.amount
    assert budget is not None
    # frontier: list of (cost, score, choices) with dominated entries removed
    frontier: list[tuple[Decimal, float, dict[str, str]]] = [(base, 0.0, {})]
    for n, cands in free:
        nxt: list[tuple[Decimal, float, dict[str, str]]] = []
        for cost, score, ch in frontier:
            for oid in cands:
                c2 = cost + price(oid)
                if c2 <= budget:
                    nxt.append((c2, score + n.scores[oid].score, {**ch, n.id: oid}))
        nxt.sort(key=lambda t: (t[0], -t[1]))
        pruned, best = [], -1.0
        for t in nxt:
            if t[1] > best:
                pruned.append(t)
                best = t[1]
        frontier = pruned
        if not frontier:
            break
    if frontier:
        cost, _, ch = max(frontier, key=lambda t: t[1])
        return Choice({**fixed, **ch}, cost, min_total, True)
    cheapest = {n.id: min(c, key=price) for n, c in free}
    return Choice({**fixed, **cheapest}, min_total, min_total, False)
