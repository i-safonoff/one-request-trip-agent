"""Candidates from many stays and trips -> the user's profile items. Pure functions.

Evidence ages: an observation loses half its weight every `half_life_days`. Several stays from ONE trip count once
(the strongest wins): a three-hotel trip is one data point about the traveller, not three. Independent trips add up.
"""
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from shared.templates import ConstraintItem, HotelHint, NUMERIC_KINDS, PriorityItem

from .extraction import Candidate

MAX_CONFIDENCE = 0.95


@dataclass
class Aggregated:
    key: str
    item: ConstraintItem | PriorityItem | HotelHint
    confidence: float
    observed: int  # distinct trips behind it
    evidence: list[dict] = field(default_factory=list)


def _age_days(at: datetime, now: datetime) -> float:
    return max(0.0, (now - at).total_seconds() / 86400)


def _effective(c: Candidate, now: datetime, half_life_days: int) -> float:
    at = c.evidence.get("at")
    at = datetime.fromisoformat(at) if isinstance(at, str) else at
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)  # SQLite hands back naive datetimes; everything stored is UTC
    return c.confidence * 0.5 ** (_age_days(at, now) / half_life_days) if at else c.confidence


def _merge_item(cands: list[Candidate]):
    """One representative item for a group that shares a key but may differ in value (min_stars 4 and 5, weights)."""
    first = cands[0].item
    if isinstance(first, ConstraintItem) and first.kind in NUMERIC_KINDS:
        values = sorted(c.item.value for c in cands)
        median = statistics.median(values)
        if first.kind in ("min_stars", "max_transfers"):
            median = int(round(median))
        elif first.kind == "min_rating":
            median = round(median * 2) / 2
        return ConstraintItem(kind=first.kind, value=median, strength=first.strength)
    if isinstance(first, PriorityItem):
        total = sum(c.confidence for c in cands) or 1.0
        weight = sum(c.item.weight * c.confidence for c in cands) / total
        return PriorityItem(key=first.key_, weight=round(min(3.0, max(0.5, weight)) * 2) / 2)
    return first


def aggregate(candidates: list[Candidate], now: datetime, half_life_days: int = 365) -> list[Aggregated]:
    groups: dict[str, list[Candidate]] = {}
    for c in candidates:
        groups.setdefault(c.item.key, []).append(c)

    out: dict[str, Aggregated] = {}
    for key, cands in groups.items():
        by_trip: dict[str, float] = {}
        for c in cands:
            trip = str(c.evidence.get("chat_id"))
            by_trip[trip] = max(by_trip.get(trip, 0.0), _effective(c, now, half_life_days))
        miss = 1.0
        for value in by_trip.values():
            miss *= 1.0 - value
        out[key] = Aggregated(key=key, item=_merge_item(cands), confidence=round(min(MAX_CONFIDENCE, 1.0 - miss), 3),
                              observed=len(by_trip), evidence=[c.evidence for c in cands])

    # The same hotel liked on one trip and avoided on another: the later experience wins, silently keeping both
    # would propose and forbid the same place.
    for key in [k for k in out if k.startswith("hotel:like:")]:
        twin = "hotel:avoid:" + key.removeprefix("hotel:like:")
        if twin in out:
            latest = lambda a: max(str(e.get("at")) for e in a.evidence)  # ISO strings sort chronologically
            loser = key if latest(out[key]) < latest(out[twin]) else twin
            del out[loser]
    return sorted(out.values(), key=lambda a: (-a.confidence, a.key))
