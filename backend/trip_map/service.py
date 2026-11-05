"""Turns a validated Trip into the flat view the map frontend consumes."""
import json
from typing import Callable

from .geocode import Coords
from .schema import Trip

Geocoder = Callable[[str], Coords | None]


def normalize(trip: Trip, geocode: Geocoder) -> dict:
    places = {}
    for pid, p in trip.places.items():
        lat, lon = p.lat, p.lon
        if (lat is None or lon is None) and p.name:
            lat, lon = geocode(p.name) or (None, None)
        places[pid] = {"id": pid, "name": p.name, "lat": lat, "lon": lon, "parent": p.parent}

    steps = []
    for nid in trip.sequence:
        n = trip.nodes[nid]
        steps.append({
            "id": nid,
            "type": n.type,
            "place": n.place,
            "from": n.from_,
            "to": n.to,
            "check_in": n.check_in,
            "check_out": n.check_out,
            "departure_date": n.departure_date,
            "mode": n.mode,
            "offer_ref": n.selected_offer_ref,
            "offer": trip.offers.get(n.selected_offer_ref) if n.selected_offer_ref else None,
            "nights": (n.check_out - n.check_in).days if n.check_in and n.check_out else None,
            # same place, or one end nested in the other => movement inside a city
            "local": bool(n.from_ and n.to and (
                n.from_ == n.to
                or places[n.from_]["parent"] == n.to
                or places[n.to]["parent"] == n.from_)),
            "extra": n.model_extra or {},
        })

    # round-trip through json to turn dates into strings
    return json.loads(json.dumps({
        "trip_id": trip.trip_id,
        "revision": trip.revision,
        "status": trip.status,
        "title": trip.title,
        "party": trip.party,
        "budget": trip.budget,
        "pricing": trip.pricing,
        "pending_questions": trip.pending_questions,
        "places": places,
        "steps": steps,
    }, default=str))
