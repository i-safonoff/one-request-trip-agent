"""Trip state and offers. Everything the agents change lives in `TripState`; prices are Decimal."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

Origin = Literal["user", "derived", "tool", "default", "agent", "history"]  # history: inherited from a past trip (shared.templates.ORIGIN)
Strength = Literal["hard", "soft"]
CheckStatus = Literal["pass", "fail", "unknown"]
DataMode = Literal["live", "mock"]


class Source(BaseModel):
    message_id: str | None = None
    quote: str | None = None
    origin: Origin = "user"


class Money(BaseModel):
    amount: Decimal
    currency: str = "RUB"
    basis: str = "stay_total"

    @field_serializer("amount")
    def _amount(self, v: Decimal) -> str:
        return f"{v:.2f}"


class Party(BaseModel):
    adults: int | None = None
    children_ages: list[int] = []
    children_unknown: int = 0  # children whose age was not given yet
    pets: list[str] = []

    @property
    def total(self) -> int:
        return (self.adults or 0) + len(self.children_ages) + self.children_unknown

    def sig(self) -> str:
        return f"{self.adults}|{','.join(map(str, sorted(self.children_ages)))}|{self.children_unknown}"


class Budget(BaseModel):
    amount: Decimal | None = None
    currency: str = "RUB"
    scope: Literal["trip", "stay", "transport"] = "trip"
    qualitative: str | None = None  # "недорого", when no number was given
    source: Source | None = None

    @field_serializer("amount")
    def _amount(self, v: Decimal | None) -> str | None:
        return None if v is None else f"{v:.2f}"


class Constraint(BaseModel):
    id: str
    kind: str
    value: Any = True
    strength: Strength = "hard"
    label: str | None = None
    source: Source | None = None


class Priority(BaseModel):
    key: str
    weight: float = 1.0
    source: Source | None = None


class Place(BaseModel):
    name: str | None = None
    lat: float | None = None
    lon: float | None = None
    tz: str | None = None
    tz_estimated: bool = False
    parent: str | None = None
    country: str | None = None
    kind: Literal["city", "airport", "station", "hub", "district"] = "city"


class OptionScore(BaseModel):
    offer_id: str
    rank: int = 0
    score: float = 0.0
    criteria: dict[str, float] = {}
    constraints: dict[str, CheckStatus] = {}
    passes_hard: bool = True


class Node(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str
    type: Literal["stay", "transport", "transfer"]
    status: Literal["empty", "searching", "options", "selected", "stale", "not_found"] = "empty"
    place: str | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    check_in: date | None = None
    check_out: date | None = None
    departure_date: date | None = None
    mode: str | None = None
    selected_offer_ref: str | None = None
    pinned: bool = False
    option_ids: list[str] = []
    scores: dict[str, OptionScore] = {}
    search_sig: str | None = None
    coverage: Literal["full", "partial"] | None = None
    excluded_offers: list[str] = []
    attrs: dict[str, Any] = {}  # e.g. estimated transfer duration


class RouteStop(BaseModel):
    place: str  # place id
    nights: int | None = None
    nights_origin: Origin = "user"


class Question(BaseModel):
    id: str
    field: str
    text: str
    options: list[str] = []
    blocking: bool = True
    status: Literal["open", "answered"] = "open"
    answer: str | None = None


class DestinationCandidate(BaseModel):
    key: str
    name: str
    country: str | None = None
    why: list[str] = []
    facts: dict[str, Any] = {}
    price_hint: Money | None = None
    price_hint_mode: DataMode | None = None


class Check(BaseModel):
    code: str
    severity: Literal["error", "warning", "info"]
    node_ids: list[str] = []
    facts: dict[str, Any] = {}
    suggestion: str | None = None


class DataNote(BaseModel):
    scope: str
    kind: Literal["mock", "partial", "estimated", "own_proposal", "unverified"]
    text: str


class CartItem(BaseModel):
    node_id: str
    offer_id: str
    title: str
    price: Money


class Cart(BaseModel):
    id: str | None = None
    status: Literal["empty", "draft", "checked", "paid"] = "empty"
    items: list[CartItem] = []
    total: Money | None = None
    order_number: str | None = None
    price_changes: list[dict[str, Any]] = []
    fingerprint: str | None = None  # what the cart was built / checked against (see domain/cartfp.py)


# ---------------------------------------------------------------- offers

class HotelInfo(BaseModel):
    provider_hotel_id: str
    name: str
    stars: int | None = None
    rating: float | None = None
    review_count: int | None = None
    address: str | None = None
    lat: float | None = None
    lon: float | None = None
    photos: list[str] = []
    amenities: list[str] = []
    check_in_time: str | None = None  # "14:00"
    check_out_time: str | None = None
    front_desk_24h: bool | None = None
    pets_allowed: bool | None = None
    kid_friendly: bool | None = None
    description: str | None = None
    aspects: dict[str, float] = {}  # guest ratings by aspect, 0..10: {"Чистота": 9.0, "Комфорт": 8.88, ...}
    pros: list[str] = []  # short quotes from guest reviews that matter for THIS trip (see domain/hotel_fit.py)
    cons: list[str] = []
    reviews_used: int = 0  # how many reviews the quotes were picked from
    feedback_loaded: bool = False


class HotelRate(BaseModel):
    room_name: str
    bed_type: str | None = None
    meal: str | None = None  # breakfast | none | other | None (unknown)
    breakfast_included: bool | None = None
    free_cancellation: bool | None = None
    free_cancellation_until: datetime | None = None  # tz-aware
    pay_at_hotel: bool | None = None
    view: str | None = None
    room_size_sqm: float | None = None
    max_occupancy: int | None = None


class Segment(BaseModel):
    mode: str
    carrier: str | None = None
    number: str | None = None
    origin_name: str
    origin_lat: float | None = None
    origin_lon: float | None = None
    destination_name: str
    destination_lat: float | None = None
    destination_lon: float | None = None
    departure: datetime  # tz-aware
    arrival: datetime  # tz-aware


class TransportInfo(BaseModel):
    mode: str  # rail | flight | bus | drive
    segments: list[Segment]
    transfers: int = 0
    duration_min: int = 0
    baggage: str | None = None
    fare_conditions: str | None = None
    overnight: bool = False


class Offer(BaseModel):
    id: str
    kind: Literal["hotel_rate", "transport"]
    source: str  # tutu_mcp | mock
    data_mode: DataMode
    retrieved_at: datetime
    price: Money
    hotel: HotelInfo | None = None
    rate: HotelRate | None = None
    transport: TransportInfo | None = None
    source_url: str | None = None
    raw_ref: str | None = None
    note: str | None = None

    def title(self) -> str:
        if self.hotel:
            return f"{self.hotel.name}, {self.rate.room_name}" if self.rate else self.hotel.name
        if self.transport:
            s = self.transport.segments
            return f"{s[0].origin_name} → {s[-1].destination_name}"
        return self.id


# ---------------------------------------------------------------- the state

class TripState(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    schema_version: int = 2
    trip_id: str | None = None
    revision: int = 0
    status: Literal["draft", "searching", "needs_input", "choosing_destination", "ready", "partial",
                    "in_cart", "booked"] = "draft"
    title: str | None = None

    scope: Literal["stay", "transport", "full"] | None = None
    kind: Literal["city", "multi_city", "country"] | None = None
    origin: str | None = None  # place id
    round_trip: bool = True
    destination_text: str | None = None  # what the user said when it is not a concrete place
    destination_unresolved: bool = False
    route: list[RouteStop] = []
    date_from: date | None = None
    date_to: date | None = None
    month_hint: str | None = None  # "2026-11" when only the month is known

    party: Party = Party()
    budget: Budget = Budget()
    constraints: list[Constraint] = []
    priorities: list[Priority] = []

    places: dict[str, Place] = {}
    nodes: dict[str, Node] = {}
    sequence: list[str] = []
    offers: dict[str, Offer] = {}

    questions: list[Question] = []
    destination_candidates: list[DestinationCandidate] = []
    checks: list[Check] = []
    data_notes: list[DataNote] = []
    facts: dict[str, Source] = {}
    cart: Cart = Cart()
    accepted_checks: list[str] = []  # keys of failed checks the user knowingly accepted (e.g. dates shifted by a late flight)
    inherited_applied: list[str] = []  # ids of accepted history items already applied to this trip
    history_hints: list[dict] = []  # {polarity: like|avoid, name, provider_hotel_id, place_name, source}: hints, not constraints

    def hard(self) -> list[Constraint]:
        return [c for c in self.constraints if c.strength == "hard"]

    def open_questions(self) -> list[Question]:
        return [q for q in self.questions if q.status == "open"]
