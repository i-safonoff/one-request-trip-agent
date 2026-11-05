"""Trip decomposition: route stops + dates + scope -> graph of stay / transport nodes.

Deterministic code, not an LLM: the planner agent decides *which places* and *how many nights*,
this module turns that into nodes with stable ids, so a changed date or an added stop only
touches the nodes that really changed and keeps the rest (selected offers, pins).
"""
from datetime import date, timedelta
from decimal import Decimal

from .geo import to_local
from .models import DataNote, Node, TripState

# Hard constraints that Tutu can apply as search filters; adding/removing one means a new search.
SEARCH_FILTERS = {"breakfast", "free_cancellation", "kid_friendly", "bed_type", "view"}


def stay_price_cap(state: TripState, node: Node) -> Decimal | None:
    """Per-night price ceiling passed to the hotel search, when the budget is about the stay alone."""
    b = state.budget
    stays = [n for n in state.nodes.values() if n.type == "stay"]
    nights = (node.check_out - node.check_in).days if node.check_in and node.check_out else 0
    priced_only_stays = all(n.type == "stay" for n in state.nodes.values() if n.type != "transfer")
    if b.amount and nights and len(stays) == 1 and (priced_only_stays or b.scope == "stay"):
        return (b.amount / nights).quantize(Decimal("1"))
    return None


def search_sig(state: TripState, node: Node) -> str:
    """Everything a node's options depend on. If it changes, the options are stale.

    Search filters count with their VALUES (view=sea and view=city are different searches). The per-night price cap
    is the last part ("cap=..."): `mark_stale` treats it separately, because a LOWER cap only needs a re-rank of
    what was found, while a higher one needs a new search (the old one never showed those options).
    """
    filt = ""
    if node.type == "stay":
        hard = ",".join(sorted(c.kind for c in state.hard() if c.kind in SEARCH_FILTERS and c.kind not in ("bed_type", "view")))
        wished = ",".join(sorted(f"{c.kind}={c.value}" for c in state.constraints if c.kind in ("bed_type", "view")))
        filt = f"{hard};{wished}"
    place = state.places.get(node.place or "")
    parts = [node.type, node.place or "", (place.name if place else "") or "",
             node.from_ or "", node.to or "", str(node.check_in), str(node.check_out),
             str(node.departure_date), state.party.sig(), filt]
    return "|".join(parts) + f"|cap={stay_price_cap(state, node) if node.type == 'stay' else None}"


def _sig_changed(state: TripState, node: Node) -> bool:
    new, old = search_sig(state, node), node.search_sig or ""
    if new == old:
        return False
    base_new, _, cap_new = new.rpartition("|cap=")
    base_old, _, cap_old = old.rpartition("|cap=")
    if base_new != base_old:
        return True
    if cap_new == cap_old or cap_old == "None":
        return False  # no ceiling before, or the same one
    return cap_new == "None" or Decimal(cap_new) > Decimal(cap_old)  # only a HIGHER ceiling needs a new search


def mark_stale(state: TripState) -> set[str]:
    stale = set()
    for n in state.nodes.values():
        if n.type == "transfer":
            continue
        if n.status in ("options", "selected", "not_found") and _sig_changed(state, n):
            n.status = "stale"
            stale.add(n.id)
    return stale


def _nights(state: TripState) -> list[int | None]:
    """Nights per stop. Unspecified stops share what remains of [date_from, date_to]."""
    stops = state.route
    nights: list[int | None] = [s.nights for s in stops]
    if state.date_from and state.date_to and stops:
        total = (state.date_to - state.date_from).days
        known = sum(n for n in nights if n)
        free = [i for i, n in enumerate(nights) if not n]
        if free and total > known:
            rest, k = total - known, len(free)
            for j, i in enumerate(free):
                share = rest // k + (1 if j < rest % k else 0)
                nights[i] = share
                stops[i].nights = share
                stops[i].nights_origin = "agent" if k > 1 else "derived"
    return nights


def rebuild_graph(state: TripState) -> None:
    """Regenerate nodes/sequence from route + dates + scope, keeping nodes whose id survives."""
    old = state.nodes
    nodes: dict[str, Node] = {}
    seq: list[str] = []
    stops = state.route
    scope = state.scope or "full"

    def keep(node: Node) -> Node:
        prev = old.get(node.id)
        if prev:
            for f in ("status", "selected_offer_ref", "pinned", "option_ids", "scores", "search_sig",
                      "coverage", "excluded_offers", "mode"):
                setattr(node, f, getattr(prev, f))
        nodes[node.id] = node
        seq.append(node.id)
        return node

    if not stops:
        state.nodes, state.sequence = {}, []
        return

    nights = _nights(state)
    day: date | None = state.date_from
    between_cities = scope in ("full", "transport")  # moves between the trip's own cities need no origin
    want_transport = between_cities and state.origin is not None  # the way there and back does
    names = {pid: (state.places[pid].name if pid in state.places else pid) for pid in
             [s.place for s in stops] + ([state.origin] if state.origin else [])}

    prev_place = state.origin
    visits: dict[str, int] = {}  # a city visited twice (Moscow -> Petersburg -> Moscow) gets two stays, not one
    moves: dict[tuple[str, str], int] = {}

    def visit_id(prefix: str, key: str, counter: dict, k) -> str:
        counter[k] = counter.get(k, 0) + 1
        return f"{prefix}{key}" if counter[k] == 1 else f"{prefix}{key}_{counter[k]}"
    for i, stop in enumerate(stops):
        if prev_place and (want_transport if i == 0 else between_cities):
            nid = "outbound" if i == 0 else visit_id("move_", f"{prev_place}__{stop.place}", moves, (prev_place, stop.place))
            keep(Node(id=nid, type="transport", from_=prev_place, to=stop.place, departure_date=day))
        if scope in ("full", "stay"):
            n = nights[i]
            cin = day
            cout = day + timedelta(days=n) if (day and n) else None
            keep(Node(id=visit_id("stay_", stop.place, visits, stop.place), type="stay", place=stop.place, check_in=cin, check_out=cout))
            day = cout
        elif scope == "transport":
            n = nights[i]
            day = day + timedelta(days=n) if (day and n) else (state.date_to if i == len(stops) - 1 else None)
        prev_place = stop.place

    if want_transport and state.round_trip and prev_place and state.origin:
        ret_day = day if scope != "transport" else (state.date_to or day)
        keep(Node(id="return", type="transport", from_=prev_place, to=state.origin, departure_date=ret_day))

    state.nodes, state.sequence = nodes, seq
    # nights chosen by the agent (not the user) are an own proposal: say so
    state.data_notes = [d for d in state.data_notes if d.scope != "nights"]
    if any(s.nights_origin == "agent" for s in stops):
        split = ", ".join(f"{names.get(s.place, s.place)} — {s.nights}" for s in stops)
        state.data_notes.append(DataNote(scope="nights", kind="own_proposal",
                                         text=f"Ночи по городам распределил я ({split}). Скажите, если нужно иначе."))


def align_arrival_dates(state: TripState) -> list:
    """After transports are chosen: a stay cannot start before the traveller arrives.

    Long-haul flights land the next day (Moscow 20:15 -> Tokyo the day after). The stay's check-in moves to
    the arrival day, its nights are kept, and everything after it shifts by the same number of days.
    Returns SetNodeDates operations (empty if all dates already fit).
    """
    from .ops import SetNodeDates
    ops, shift = [], 0
    nodes = [state.nodes[i] for i in state.sequence if state.nodes[i].type != "transfer"]
    for i, n in enumerate(nodes):
        if n.type == "transport" and shift and n.departure_date:
            ops.append(SetNodeDates(node_id=n.id, departure_date=n.departure_date + timedelta(days=shift)))
        if n.type != "stay" or not n.check_in:
            continue
        prev = nodes[i - 1] if i > 0 else None
        o = state.offers.get(prev.selected_offer_ref or "") if prev and prev.type == "transport" else None
        extra = 0
        if o and o.transport:
            place = state.places.get(n.place or "")
            arr = to_local(o.transport.segments[-1].arrival, place.tz if place else None).date()
            arr_shifted_in = n.check_in + timedelta(days=shift)
            extra = max((arr - arr_shifted_in).days, 0)
        total = shift + extra
        if total:
            ops.append(SetNodeDates(node_id=n.id, check_in=n.check_in + timedelta(days=total),
                                    check_out=n.check_out + timedelta(days=total) if n.check_out else None))
        shift = total
    return ops
