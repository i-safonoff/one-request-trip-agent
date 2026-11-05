"""Contract for inherited trip preferences: what the history module proposes and the agents module consumes.

An *item* is one reusable piece of a past trip. Items speak the agents' own vocabulary: a constraint item becomes
their `Constraint(kind, value, strength)`, a priority item their `Priority(key, weight)`, so applying one needs no
translation layer. Only durable things are inherited (criteria, soft priorities, hotel likes/dislikes, the shape of
a route). Dates, prices, availability and offers are never inherited: they go stale the day the trip ends.

The vocabulary mirrors backend/agents/domain/scoring.py (STAY_KINDS, TRANSPORT_KINDS, PRIORITY_KEYS).
Anything outside it is rejected, so neither a model nor a bad client can poison someone's profile with junk.
"""
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, model_validator

STAY_CONSTRAINT_KINDS = frozenset({"breakfast", "free_cancellation", "pets_allowed", "kid_friendly",
                                   "min_stars", "min_rating"})
TRANSPORT_CONSTRAINT_KINDS = frozenset({"max_travel_hours", "max_transfers", "exclude_modes", "only_modes",
                                        "avoid_overnight"})
CONSTRAINT_KINDS = STAY_CONSTRAINT_KINDS | TRANSPORT_CONSTRAINT_KINDS
PRIORITY_KEYS = frozenset({"price", "rating", "location", "cancellation", "meal", "duration", "transfers",
                           "schedule", "stars"})
# What a traveller can comment on after a stay. A subset of PRIORITY_KEYS, so feedback maps straight to priorities.
ASPECTS = ("price", "location", "rating", "cancellation", "meal", "stars")
NUMERIC_KINDS = {"min_stars": (1, 5), "min_rating": (5.0, 10.0), "max_travel_hours": (0.5, 48.0), "max_transfers": (0, 6)}

# `Source.origin` the agents should record for inherited items. Their Origin literal needs this one extra value.
ORIGIN = "history"

Status = Literal["proposed", "accepted", "dismissed"]


class ConstraintItem(BaseModel):
    type: Literal["constraint"] = "constraint"
    kind: str
    value: Any = True
    strength: Literal["soft", "hard"] = "soft"  # inherited items are soft: the past is a hint, not a requirement

    @model_validator(mode="after")
    def _valid(self):
        if self.kind not in CONSTRAINT_KINDS:
            raise ValueError(f"unknown constraint kind {self.kind!r}")
        if self.kind in NUMERIC_KINDS:
            lo, hi = NUMERIC_KINDS[self.kind]
            if not isinstance(self.value, (int, float)) or isinstance(self.value, bool) or not lo <= self.value <= hi:
                raise ValueError(f"{self.kind} must be a number in [{lo}, {hi}]")
        elif self.kind in ("exclude_modes", "only_modes"):
            if not (isinstance(self.value, list) and self.value and all(isinstance(m, str) for m in self.value)):
                raise ValueError(f"{self.kind} must be a non-empty list of modes")
        elif self.value is not True and self.value is not False:
            raise ValueError(f"{self.kind} must be a boolean")
        return self

    @property
    def key(self) -> str:
        return f"constraint:{self.kind}"


class PriorityItem(BaseModel):
    type: Literal["priority"] = "priority"
    key_: str = Field(alias="key")
    weight: float = Field(default=1.5, ge=0.5, le=3.0)
    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _valid(self):
        if self.key_ not in PRIORITY_KEYS:
            raise ValueError(f"unknown priority key {self.key_!r}")
        return self

    @property
    def key(self) -> str:
        return f"priority:{self.key_}"


class HotelHint(BaseModel):
    """A hotel the traveller liked or wants to avoid. A hint for the agents, not a constraint: the hotel may not exist
    in the next city or at the next price."""
    type: Literal["hotel"] = "hotel"
    polarity: Literal["like", "avoid"]
    provider_hotel_id: str | None = None
    name: str
    place_name: str | None = None  # where it is: a hint only makes sense for the same destination

    @property
    def key(self) -> str:
        return f"hotel:{self.polarity}:{(self.provider_hotel_id or self.name).lower()}"


Item = Annotated[ConstraintItem | PriorityItem | HotelHint, Field(discriminator="type")]
item_adapter: TypeAdapter[Item] = TypeAdapter(Item)


def parse_item(data: dict) -> ConstraintItem | PriorityItem | HotelHint:
    return item_adapter.validate_python(data)


def to_agent_dict(item: ConstraintItem | PriorityItem | HotelHint, reason: str, ident: str) -> dict:
    """The item as the agents' own structures (`Constraint` / `Priority`), with provenance for the user-facing
    "why". Hotel hints have no agent-side equivalent yet and come back under `hint`, to be shown, not enforced."""
    source = {"origin": ORIGIN, "quote": reason}
    if isinstance(item, ConstraintItem):
        return {"id": ident, "kind": item.kind, "value": item.value, "strength": item.strength,
                "label": reason, "source": source}
    if isinstance(item, PriorityItem):
        return {"key": item.key_, "weight": item.weight, "source": source}
    return {"hint": "hotel", "polarity": item.polarity, "provider_hotel_id": item.provider_hotel_id,
            "name": item.name, "place_name": item.place_name, "source": source}


def apply_items(config: dict, accepted: list[tuple[dict, str, str]]) -> dict:
    """Merge accepted items into a trip config (agents' `constraints` / `priorities`). Pure, returns a new dict.

    `accepted` is [(item_dict, reason, id)]. Rules, so inheritance never overrides the user:
      * a constraint/priority the config already has (same kind / key) wins: what the user said in THIS trip beats
        anything from the past;
      * hotel hints go to `history_hints`, never to constraints.
    """
    out = {**config, "constraints": list(config.get("constraints") or []), "priorities": list(config.get("priorities") or []),
           "history_hints": list(config.get("history_hints") or [])}
    have_kinds = {c.get("kind") for c in out["constraints"]}
    have_keys = {p.get("key") for p in out["priorities"]}
    for raw, reason, ident in accepted:
        item = parse_item(raw)
        agent = to_agent_dict(item, reason, ident)
        if isinstance(item, ConstraintItem) and item.kind not in have_kinds:
            out["constraints"].append(agent)
            have_kinds.add(item.kind)
        elif isinstance(item, PriorityItem) and item.key_ not in have_keys:
            out["priorities"].append(agent)
            have_keys.add(item.key_)
        elif isinstance(item, HotelHint):
            out["history_hints"].append(agent)
    return out
