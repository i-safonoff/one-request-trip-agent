"""Reads a saved trip plan into plain facts. The only place that knows plan formats.

Two shapes exist today: the agents' `TripState` (offers are structured: `hotel`, `rate`) and the flat graph config the
demo/mock plans use (offers are dicts with an `amenities` list). Both come out as the same `StayFacts`.
"""
from dataclasses import dataclass
from datetime import date


@dataclass
class StayFacts:
    node_id: str
    place_name: str | None
    nights: int | None
    hotel_id: str | None
    name: str | None
    stars: int | None
    rating: float | None
    breakfast: bool | None  # None = unknown, which is not the same as "no"
    free_cancellation: bool | None
    kid_friendly: bool | None
    pets_allowed: bool | None


def _date(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _place_name(state: dict, place_id: str | None) -> str | None:
    place = (state.get("places") or {}).get(place_id or "") or {}
    return place.get("name")


def _has(amenities: list, *needles: str) -> bool:
    text = " ".join(str(a).lower() for a in amenities or [])
    return any(n in text for n in needles)


def _stay_facts(node_id: str, node: dict, state: dict) -> StayFacts:
    offer = (state.get("offers") or {}).get(node.get("selected_offer_ref") or "") or {}
    hotel, rate = offer.get("hotel"), offer.get("rate")
    if hotel is not None or rate is not None:  # TripState
        hotel, rate = hotel or {}, rate or {}
        facts = dict(hotel_id=hotel.get("provider_hotel_id"), name=hotel.get("name"), stars=hotel.get("stars"),
                     rating=hotel.get("rating"), breakfast=rate.get("breakfast_included"),
                     free_cancellation=rate.get("free_cancellation"), kid_friendly=hotel.get("kid_friendly"),
                     pets_allowed=hotel.get("pets_allowed"))
    else:  # flat graph config
        amenities = offer.get("amenities") or []
        facts = dict(hotel_id=None, name=offer.get("name"), stars=offer.get("stars"), rating=offer.get("rating"),
                     breakfast=True if _has(amenities, "завтрак", "breakfast") else None,
                     free_cancellation=True if _has(amenities, "отмен", "free cancel") else None,
                     kid_friendly=True if _has(amenities, "дет", "kid") else None,
                     pets_allowed=True if _has(amenities, "животн", "pets") else None)
    ci, co = _date(node.get("check_in")), _date(node.get("check_out"))
    return StayFacts(node_id=node_id, place_name=_place_name(state, node.get("place")),
                     nights=(co - ci).days if ci and co else None, **facts)


def stays(state: dict) -> list[StayFacts]:
    """Stays that have a chosen offer, in trip order. Stays without one say nothing about taste."""
    order = state.get("sequence") or list((state.get("nodes") or {}))
    out = []
    for nid in order:
        node = (state.get("nodes") or {}).get(nid) or {}
        if node.get("type") == "stay" and node.get("selected_offer_ref") and (state.get("offers") or {}).get(node["selected_offer_ref"]):
            out.append(_stay_facts(nid, node, state))
    return out


def date_range(state: dict) -> tuple[date | None, date | None]:
    days = [d for n in (state.get("nodes") or {}).values() for k in ("check_in", "check_out", "departure_date")
            if (d := _date(n.get(k)))]
    return (min(days), max(days)) if days else (None, None)
