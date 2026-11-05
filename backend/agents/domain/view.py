"""`view_json`: everything the front end needs for map, cards and modals, built from one TripState.

It is a superset of the `trip_map` plan format (places + nodes + sequence + offers), so the map
module keeps working on it, plus the explanations (why / trade-offs / risks), checks, pricing,
questions, destination candidates and the cart. Built by code, never by an LLM.
"""
from __future__ import annotations

from typing import Any

from . import fmt
from .budget import compute_pricing
from .models import Node, Offer, TripState
from .turn import node_label, recommendations, risk_text, understood

VIEW_SCHEMA_VERSION = 2


def _offer_view(o: Offer) -> dict[str, Any]:
    d = o.model_dump(mode="json")
    d["title"] = o.title()
    # aliases understood by the trip_map frontend and by simple cards
    d["price_total"] = f"{o.price.amount:.2f}"
    d["currency"] = o.price.currency
    if o.hotel:
        d.update(name=o.hotel.name, lat=o.hotel.lat, lon=o.hotel.lon, address=o.hotel.address, stars=o.hotel.stars,
                 rating=o.hotel.rating, reviews_count=o.hotel.review_count, photos=o.hotel.photos,
                 amenities=o.hotel.amenities)
    if o.transport:
        t = o.transport
        d["details"] = f"{fmt.duration(t.duration_min)}" + (", без пересадок" if not t.transfers else f", пересадок: {t.transfers}")
    return d


def _geometry(state: TripState, n: Node) -> tuple[dict | None, str | None]:
    if n.type == "transfer":
        a, b = state.places.get(n.from_ or ""), state.places.get(n.to or "")
        if a and b and a.lat is not None and b.lat is not None:
            return {"type": "LineString", "coordinates": [[a.lon, a.lat], [b.lon, b.lat]]}, "routed"
        return None, None
    o = state.offers.get(n.selected_offer_ref or "")
    known = o and o.transport and any(g.origin_lat is not None or g.destination_lat is not None for g in o.transport.segments)
    if n.type == "transport" and known:
        segs = o.transport.segments
        a, b = state.places.get(n.from_ or ""), state.places.get(n.to or "")
        pts: list[list[float]] = []
        for i, seg in enumerate(segs):
            if seg.origin_lat is not None:
                start = [seg.origin_lon, seg.origin_lat]
            elif i == 0 and a and a.lat is not None:
                start = [a.lon, a.lat]  # the first airport has no coordinates: begin at the city, not at the first known stop
            else:
                start = None
            if start and (not pts or pts[-1] != start):
                pts.append(start)
            if seg.destination_lat is not None:
                pts.append([seg.destination_lon, seg.destination_lat])
            elif i == len(segs) - 1 and b and b.lat is not None:
                pts.append([b.lon, b.lat])
        if len(pts) >= 2:
            return {"type": "LineString", "coordinates": pts}, "great_circle" if o.transport.mode == "flight" else "stations"
    if n.type == "transport":
        a, b = state.places.get(n.from_ or ""), state.places.get(n.to or "")
        if a and b and a.lat is not None and b.lat is not None:
            # airports without coordinates: the flight is still an arc between the two cities, not a ruler line
            mode = o.transport.mode if o and o.transport else n.mode
            return {"type": "LineString", "coordinates": [[a.lon, a.lat], [b.lon, b.lat]]}, "great_circle" if mode == "flight" else "straight"
    return None, None


def build_view(state: TripState) -> dict[str, Any]:
    recs = {r.node_id: r.model_dump() for r in recommendations(state)}
    nodes: dict[str, Any] = {}
    for nid, n in state.nodes.items():
        geom, gstatus = _geometry(state, n)
        d = n.model_dump(mode="json", by_alias=True, exclude={"scores", "option_ids"})
        d["label"] = node_label(state, n)
        d["options"] = [sc.model_dump(mode="json") for sc in sorted(n.scores.values(), key=lambda s: s.rank)]
        d["option_ids"] = list(n.option_ids)
        d["geometry"], d["geometry_status"] = geom, gstatus
        if nid in recs:
            d["recommendation"] = recs[nid]
        nodes[nid] = d
    return {
        "schema_version": VIEW_SCHEMA_VERSION,
        "trip_id": state.trip_id, "revision": state.revision, "status": state.status, "title": state.title,
        "trip": {"scope": state.scope, "kind": state.kind, "origin": state.origin, "round_trip": state.round_trip,
                 "date_from": str(state.date_from) if state.date_from else None,
                 "date_to": str(state.date_to) if state.date_to else None,
                 "party": state.party.model_dump(), "destination_text": state.destination_text},
        "budget": state.budget.model_dump(mode="json"),
        "pricing": compute_pricing(state),
        "party": state.party.model_dump(),
        "priorities": [p.model_dump(mode="json") for p in state.priorities],
        "constraints": [c.model_dump(mode="json") for c in state.constraints],
        "understood": [u.model_dump() for u in understood(state)],
        "places": {k: v.model_dump(mode="json") for k, v in state.places.items()},
        "sequence": list(state.sequence),
        "nodes": nodes,
        "offers": {k: _offer_view(v) for k, v in state.offers.items()},
        "checks": [{**c.model_dump(mode="json"), "text": risk_text(c)} for c in state.checks],
        "data_notes": [d.model_dump(mode="json") for d in state.data_notes],
        "questions": [q.model_dump(mode="json") for q in state.open_questions()],
        "destination_candidates": [c.model_dump(mode="json") for c in state.destination_candidates],
        "cart": state.cart.model_dump(mode="json"),
    }
