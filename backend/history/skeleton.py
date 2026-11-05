"""Trip templates: a reusable fragment of a trip config, `extends`, and turning one into a fresh draft. Pure functions.

What a template carries: the shape of the route (stops, nights, how the legs were travelled) and soft criteria.
What it never carries: dates, prices, offers, party, budget. Party and budget are the context of a trip, not the
traveller's taste; copying them silently is how a family holiday template produces a wrong solo business trip.

`extends` has depth 1 on purpose: a template may have one parent. Chains and multiple parents make "where did this
setting come from?" unanswerable. Resolution flattens child over parent and records provenance per item.
"""
import re
from datetime import date, timedelta

from pydantic import BaseModel

from shared.templates import ConstraintItem, HotelHint, PriorityItem, apply_items

VERSION = 1


class Stop(BaseModel):
    place_name: str
    nights: int | None = None


class Leg(BaseModel):
    from_name: str | None = None  # None for the traveller's home, which a template cannot know
    to_name: str | None = None
    mode: str | None = None


class TemplateFragment(BaseModel):
    version: int = VERSION
    route: list[Stop] = []
    legs: list[Leg] = []
    # how stops and legs alternate in the trip ("leg", "stop", "stop", "leg", ...); kept because a trip's real order
    # (flight, airport transfer, city, train, city, flight home) is not a plain alternation
    order: list[str] = []
    constraints: list[ConstraintItem] = []
    priorities: list[PriorityItem] = []
    hotel_hints: list[HotelHint] = []
    remove: list[str] = []  # in a child: constraint kinds / priority keys to drop from the parent


def skeleton_from_state(state: dict) -> TemplateFragment:
    """Route shape and the criteria a finished trip ended with. Prices, dates and offers are dropped on purpose."""
    places = state.get("places") or {}
    nodes = state.get("nodes") or {}
    route, legs, order = [], [], []
    for nid in state.get("sequence") or list(nodes):
        n = nodes.get(nid) or {}
        name = lambda pid: (places.get(pid or "") or {}).get("name")
        if n.get("type") == "stay" and name(n.get("place")):
            nights = None
            try:
                nights = (date.fromisoformat(str(n["check_out"])[:10]) - date.fromisoformat(str(n["check_in"])[:10])).days
            except (KeyError, ValueError, TypeError):
                pass
            route.append(Stop(place_name=name(n["place"]), nights=nights if nights and nights > 0 else None))
            order.append("stop")
        elif n.get("type") in ("transport", "transfer"):
            legs.append(Leg(from_name=name(n.get("from")), to_name=name(n.get("to")), mode=n.get("mode")))
            order.append("leg")

    constraints, priorities = [], []
    for c in state.get("constraints") or []:  # criteria of the finished trip; anything outside the vocabulary is dropped
        try:
            constraints.append(ConstraintItem(kind=c["kind"], value=c.get("value", True), strength="soft"))
        except (KeyError, ValueError):
            continue
    for p in state.get("priorities") or []:
        try:
            priorities.append(PriorityItem(key=p["key"], weight=min(3.0, max(0.5, float(p.get("weight", 1.5))))))
        except (KeyError, ValueError, TypeError):
            continue
    return TemplateFragment(route=route, legs=legs, order=order, constraints=constraints, priorities=priorities)


class ResolvedFragment(BaseModel):
    fragment: TemplateFragment
    provenance: dict[str, str]  # item key / "route" -> "parent" | "child"


def resolve(child: TemplateFragment, parent: TemplateFragment | None = None) -> ResolvedFragment:
    """Child over parent. The route is atomic (a child's route replaces the parent's whole, lists are not spliced);
    constraints and priorities merge by kind / key with the child winning; `child.remove` drops parent items."""
    if parent is None:
        prov = {"route": "child", **{c.key: "child" for c in child.constraints}, **{p.key: "child" for p in child.priorities}}
        return ResolvedFragment(fragment=child.model_copy(update={"remove": []}), provenance=prov)

    prov: dict[str, str] = {}
    own_route = bool(child.route or child.legs)
    src = child if own_route else parent
    route, legs, order = src.route, src.legs, src.order
    prov["route"] = "child" if own_route else "parent"

    constraints: dict[str, ConstraintItem] = {}
    priorities: dict[str, PriorityItem] = {}
    for who, frag in (("parent", parent), ("child", child)):
        for c in frag.constraints:
            constraints[c.kind] = c
            prov[c.key] = who
        for p in frag.priorities:
            priorities[p.key_] = p
            prov[p.key] = who
    for dropped in child.remove:
        constraints.pop(dropped, None)
        priorities.pop(dropped, None)
        prov.pop(f"constraint:{dropped}", None)
        prov.pop(f"priority:{dropped}", None)
    hints = {h.key: h for h in [*parent.hotel_hints, *child.hotel_hints]}
    return ResolvedFragment(
        fragment=TemplateFragment(route=route, legs=legs, order=order, constraints=list(constraints.values()),
                                  priorities=list(priorities.values()), hotel_hints=list(hints.values())),
        provenance=prov)


def _slug(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9а-яё]+", "_", name.lower()).strip("_") or "place"
    slug, i = base, 2
    while slug in taken:
        slug, i = f"{base}_{i}", i + 1
    taken.add(slug)
    return slug


def instantiate(frag: TemplateFragment, template_name: str, start: date | None = None) -> dict:
    """A fresh draft config (graph format) from a resolved fragment: the same route, no offers, no prices.
    With `start`, dates are laid out from it using each stop's nights; unknown nights leave later dates empty."""
    taken: set[str] = set()
    places: dict[str, dict] = {}
    ids: dict[str | None, str] = {}

    def place_id(name: str | None) -> str:
        if name not in ids:
            pid = _slug(name or "origin", taken)
            ids[name] = pid
            places[pid] = {"name": name}
        return ids[name]

    nodes: dict[str, dict] = {}
    sequence: list[str] = []
    route_out: list[dict] = []
    legs, stops = list(frag.legs), list(frag.route)
    order = frag.order if sorted(frag.order) == sorted(["leg"] * len(legs) + ["stop"] * len(stops)) else None
    if order is None:  # a hand-made fragment without a recorded order: alternate leg, stop, leg, stop, ...
        order = [k for i in range(max(len(legs), len(stops))) for k in (("leg",) * (i < len(legs)) + ("stop",) * (i < len(stops)))]
    legs_it, stops_it = iter(legs), iter(stops)
    steps = [(k, next(legs_it) if k == "leg" else next(stops_it)) for k in order]
    cursor = start
    for kind, step in steps:
        if kind == "leg":
            nid = _slug(f"{step.from_name or 'origin'}_{step.to_name or 'origin'}", taken)
            nodes[nid] = {"id": nid, "type": "transport", "from": place_id(step.from_name), "to": place_id(step.to_name),
                          "mode": step.mode, "departure_date": cursor.isoformat() if cursor else None,
                          "selected_offer_ref": None, "status": "empty"}
        else:
            pid = place_id(step.place_name)
            nid = _slug(f"stay_{step.place_name}", taken)
            check_out = cursor + timedelta(days=step.nights) if cursor and step.nights else None
            nodes[nid] = {"id": nid, "type": "stay", "place": pid, "check_in": cursor.isoformat() if cursor else None,
                          "check_out": check_out.isoformat() if check_out else None, "selected_offer_ref": None,
                          "status": "empty"}
            route_out.append({"place": pid, "nights": step.nights})
            cursor = check_out  # None when nights are unknown: later dates stay empty
        sequence.append(nid)

    config = {"schema_version": 1, "status": "draft", "title": f"Как в «{template_name}»", "places": places,
              "nodes": nodes, "sequence": sequence, "route": route_out, "offers": {},
              "provenance": {"template": template_name, "note": "route shape and soft criteria only; no dates "
                                                              "(unless given), prices or offers are inherited"}}
    reason = f"Из шаблона «{template_name}»"
    accepted = [(c.model_dump(), reason, f"tpl:{c.key}") for c in frag.constraints]
    accepted += [(p.model_dump(by_alias=True), reason, f"tpl:{p.key}") for p in frag.priorities]
    accepted += [(h.model_dump(), reason, f"tpl:{h.key}") for h in frag.hotel_hints]
    return apply_items(config, accepted)
