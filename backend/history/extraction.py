"""Feedback + stay facts -> candidate items. Pure functions, no I/O.

Design rules, each one a guard against inferring something wrong about a person:
  * a candidate is a *soft* item with a confidence below 1; one observation never makes a requirement;
  * facts only count when known: "breakfast unknown" is not "no breakfast";
  * "didn't like X" is turned into the need behind it (didn't like the price -> price matters more), never into
    "avoid this hotel's features";
  * a plain thumbs-up without reasons yields only weak candidates; they matter only if the pattern repeats.
"""
import math
from dataclasses import dataclass

from shared.templates import ASPECTS, ConstraintItem, HotelHint, PriorityItem

from .facts import StayFacts


@dataclass
class Candidate:
    item: ConstraintItem | PriorityItem | HotelHint
    confidence: float
    evidence: dict  # who/what produced it: chat_id, node_id, feedback_id, trip_title, hotel_name, at


def _hotel_hint(f: StayFacts, polarity: str) -> HotelHint | None:
    if not (f.hotel_id or f.name):
        return None
    return HotelHint(polarity=polarity, provider_hotel_id=f.hotel_id, name=f.name or f.hotel_id,
                     place_name=f.place_name)


def extract(facts: StayFacts, sentiment: int, aspects: dict[str, int], evidence: dict, weight: float = 1.0) -> list[Candidate]:
    """Candidates from one stay. `weight` < 1 for feedback on the whole trip (less specific than on this stay)."""
    out: list[Candidate] = []

    def add(item, confidence: float):
        out.append(Candidate(item=item, confidence=round(min(confidence * weight, 0.9), 3), evidence=evidence))

    if sentiment > 0 and (h := _hotel_hint(facts, "like")):
        add(h, 0.6)
    if sentiment < 0 and (h := _hotel_hint(facts, "avoid")):
        add(h, 0.7)

    for aspect in ASPECTS:
        s = aspects.get(aspect, 0)
        if not s:
            continue
        if aspect == "meal":
            if s > 0 and facts.breakfast is True:
                add(ConstraintItem(kind="breakfast", value=True), 0.6)
            elif s < 0:
                add(PriorityItem(key="meal", weight=2.0), 0.5)
                if facts.breakfast is not True:
                    add(ConstraintItem(kind="breakfast", value=True), 0.5)
        elif aspect == "cancellation":
            if (s > 0 and facts.free_cancellation is True) or s < 0:
                add(ConstraintItem(kind="free_cancellation", value=True), 0.6 if s > 0 else 0.5)
        elif aspect == "price" and s < 0:
            add(PriorityItem(key="price", weight=2.0), 0.55)
        elif aspect == "location":
            add(PriorityItem(key="location", weight=1.5 if s > 0 else 2.0), 0.5 if s > 0 else 0.55)
        elif aspect == "rating":
            if facts.rating is not None:
                value = float(math.floor(facts.rating)) if s > 0 else min(9.0, round(facts.rating + 0.5))
                if 5.0 <= value <= 10.0 and (s < 0 or facts.rating >= 8):
                    add(ConstraintItem(kind="min_rating", value=value), 0.4 if s > 0 else 0.5)
            elif s < 0:
                add(PriorityItem(key="rating", weight=2.0), 0.5)
        elif aspect == "stars":
            if facts.stars is not None:
                value = facts.stars if s > 0 else min(5, facts.stars + 1)
                add(ConstraintItem(kind="min_stars", value=value), 0.4 if s > 0 else 0.5)
            elif s < 0:
                add(PriorityItem(key="stars", weight=2.0), 0.5)

    if sentiment > 0 and not aspects:  # a bare thumbs-up: weak hints from what the stay had
        if facts.breakfast is True:
            add(ConstraintItem(kind="breakfast", value=True), 0.35)
        if facts.free_cancellation is True:
            add(ConstraintItem(kind="free_cancellation", value=True), 0.35)
        if facts.stars is not None:
            add(ConstraintItem(kind="min_stars", value=facts.stars), 0.3)
    return out


