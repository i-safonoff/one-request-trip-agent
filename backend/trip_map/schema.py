"""Trip-graph schema: `places` + `nodes` + `sequence`, plus optional `offers`.

Only the graph skeleton is validated strictly; everything the planner may add
(offer details, extra node fields, provenance, ...) passes through untouched.
"""
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Place(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    parent: str | None = None  # e.g. an airport or district inside a city place


class Node(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    type: str
    place: str | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    check_in: date | None = None
    check_out: date | None = None
    departure_date: date | None = None
    # how the move is made: "drive" / "walk" / "bike" are routed along roads,
    # anything else ("flight", "rail", missing) is drawn as a straight/great-circle line
    mode: str | None = None
    selected_offer_ref: str | None = None


class Trip(BaseModel):
    model_config = ConfigDict(extra="allow")
    trip_id: str | None = None
    revision: int | None = None
    status: str | None = None
    title: str | None = None
    party: dict[str, Any] = {}
    budget: dict[str, Any] = {}
    pricing: dict[str, Any] = {}
    pending_questions: list[Any] = []
    places: dict[str, Place]
    nodes: dict[str, Node]
    sequence: list[str]
    offers: dict[str, dict[str, Any]] = {}

    @model_validator(mode="after")
    def refs_exist(self):
        for nid in self.sequence:
            if nid not in self.nodes:
                raise ValueError(f"sequence references unknown node '{nid}'")
        for nid, n in self.nodes.items():
            for ref in (n.place, n.from_, n.to):
                if ref is not None and ref not in self.places:
                    raise ValueError(f"node '{nid}' references unknown place '{ref}'")
            if n.check_in and n.check_out and n.check_out <= n.check_in:
                raise ValueError(f"node '{nid}': check_out must be after check_in")
        return self
