"""Hotel sources: Tutu MCP (live) and a deterministic demo provider (explicitly labelled mock).

Both return normalised `Offer`s: one offer = one room rate of one hotel, price for the whole stay.
Normalisation follows the real `search_hotels` / `get_offer_details` response shapes
(see tests/fixtures/tutu_*.json, captured from the real server).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

from ..domain import guard
from ..domain.hotel_fit import Review
from ..domain.models import HotelInfo, HotelRate, Money, Offer, Place
from .mcp_client import McpClient, McpError, SourceUnavailable

log = logging.getLogger("hotels")


@dataclass
class HotelQuery:
    city: str
    check_in: date
    check_out: date
    adults: int
    children_ages: list[int] = field(default_factory=list)
    breakfast: bool = False
    free_cancellation: bool = False
    kid_friendly: bool = False
    price_max_per_night: Decimal | None = None
    center: Place | None = None
    limit_hotels: int = 5
    bed_type: str | None = None  # double | twin | single: rooms with this bed come first
    view: str | None = None  # sea | city | mountain | river | lake | park


@dataclass
class HotelResult:
    offers: list[Offer]
    coverage: str  # full | partial
    note: str | None = None
    resolved_name: str | None = None


class HotelProvider(Protocol):
    async def search(self, q: HotelQuery) -> HotelResult: ...
    async def reviews(self, hotel_id: str, q: HotelQuery) -> list[Review]: ...


def _stable_id(prefix: str, *parts: Any) -> str:
    return f"{prefix}_{hashlib.sha1('|'.join(map(str, parts)).encode()).hexdigest()[:10]}"


def _dt(v: str | None) -> datetime | None:
    if not v:
        return None
    d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _amenity_names(hotel: dict) -> list[str]:
    names: list[str] = []
    for g in hotel.get("amenity_groups") or []:
        names.extend(g.get("amenities") or [])
    return list(dict.fromkeys(names))


def _has(amenities: list[str], *needles: str) -> bool:
    return any(n in a.lower() for a in amenities for n in needles)


def normalize_hotel(hotel: dict) -> HotelInfo:
    am = _amenity_names(hotel)
    loc = hotel.get("location") or {}
    aspects = {guard.scrub(a.get("text"), 40): float(a["rating"]) for a in (hotel.get("review_summary") or {}).get("aspects") or []
               if a.get("text") and a.get("rating") is not None}
    return HotelInfo(
        provider_hotel_id=str(hotel.get("hotel_id")), name=guard.safe_name(hotel.get("name")),
        stars=hotel.get("stars"), rating=hotel.get("rating") if hotel.get("review_count") else None,  # a 0 with no reviews is "unrated", not "bad"
        review_count=hotel.get("review_count"),
        address=guard.safe_name(hotel.get("address")) or None, lat=loc.get("lat"), lon=loc.get("lng"),
        aspects=aspects,
        photos=list(hotel.get("photos") or [])[:6], amenities=[g for g in (guard.scrub(a, 60) for a in am[:24]) if g],
        check_in_time=(hotel.get("check_in_time") or "")[:5] or None,
        check_out_time=(hotel.get("check_out_time") or "")[:5] or None,
        # unknown stays None: a missing amenity is not proof that the hotel lacks it
        front_desk_24h=True if _has(am, "круглосуточная стойка") else None,
        pets_allowed=True if _has(am, "с домашними животными", "можно с питомцами") else None,
        kid_friendly=True if _has(am, "детск", "для детей") else None,
    )


def normalize_rates(details: dict, retrieved_at: datetime, max_rooms: int = 2,
                    need_breakfast: bool = False, need_cancel: bool = False,
                    bed_type: str | None = None, view: str | None = None) -> list[Offer]:
    """One offer per room: the cheapest rate that satisfies the requested filters. Rooms with the wished bed / view
    come first (they are preferences, not filters: other rooms stay as alternatives)."""
    info = normalize_hotel(details["hotel"])
    offers: list[tuple[tuple[int, Decimal], Offer]] = []
    for room in details.get("rooms") or []:
        rates = [r for r in room.get("rates") or []
                 if (not need_breakfast or r.get("breakfast_included"))
                 and (not need_cancel or r.get("free_cancellation"))]
        if not rates:
            continue
        r = min(rates, key=lambda x: Decimal(str(x["price"]["amount"])))
        meal = r.get("meal") or {}
        price = Decimal(str(r["price"]["amount"]))
        mismatch = int(bool(bed_type and room.get("bed_type") != bed_type) or bool(view and room.get("view") != view))
        offers.append(((mismatch, price), Offer(
            id=_stable_id("tutu", info.provider_hotel_id, room.get("room_name"), room.get("bed_summary"), meal.get("type"), r.get("free_cancellation")),
            kind="hotel_rate", source="tutu_mcp", data_mode="live", retrieved_at=retrieved_at,
            price=Money(amount=price, currency=r["price"].get("currency", "RUB"), basis=r.get("price_basis", "stay_total")),
            hotel=info,
            rate=HotelRate(room_name=guard.safe_name(room.get("room_name")) or "Номер", bed_type=room.get("bed_type"),
                           meal=meal.get("type"), breakfast_included=r.get("breakfast_included"),
                           free_cancellation=r.get("free_cancellation"),
                           free_cancellation_until=_dt(r.get("free_cancellation_until")),
                           pay_at_hotel=r.get("pay_at_hotel"), view=room.get("view"),
                           room_size_sqm=room.get("room_size_sqm"), max_occupancy=room.get("max_occupancy")),
            raw_ref=f"tutu:{info.provider_hotel_id}:{r.get('offerpack_hash')}",
        )))
    offers.sort(key=lambda t: t[0])
    seen: set[str] = set()
    unique = [o for _, o in offers if not (o.id in seen or seen.add(o.id))]  # same room+meal+cancel: keep the cheapest
    return unique[:max_rooms]


def normalize_reviews(details: dict) -> list[Review]:
    """Guest reviews from `get_offer_details(view='reviews')`: {hotel: {reviews: {reviews: [...]}}}."""
    raw = ((details.get("hotel") or {}).get("reviews") or {}).get("reviews") or []
    out: list[Review] = []
    for r in raw:
        texts = {t.get("sentiment"): t.get("text") for t in r.get("texts") or []}
        lineup = " ".join(f"{x.get('id', '')} {x.get('text', '')}" for x in (r.get("trip") or {}).get("lineup") or []).lower()
        out.append(Review(rating=r.get("rating"), created=_dt(r.get("created_at")), lineup=lineup,
                          pros=texts.get("pros"), cons=texts.get("cons")))
    return out


class TutuHotels:
    def __init__(self, client: McpClient):
        self.mcp = client

    async def search(self, q: HotelQuery) -> HotelResult:
        nights = (q.check_out - q.check_in).days
        args: dict[str, Any] = {"city_name": q.city, "check_in": q.check_in.isoformat(), "check_out": q.check_out.isoformat(),
                                "adults": q.adults, "page": 1, "page_size": 10, "view": "compact"}
        if q.breakfast:
            args["breakfast_included"] = True
        if q.free_cancellation:
            args["free_cancellation"] = True
        if q.kid_friendly:
            args["hotel_amenities"] = ["kid_friendly"]
        if q.price_max_per_night and nights:
            args["price_max"] = int(q.price_max_per_night)  # per night, by Tutu's contract
        if q.children_ages:
            args["children_ages"] = q.children_ages
        try:
            found = await self.mcp.call("search_hotels", args)
        except McpError as e:
            if q.children_ages and "children" in str(e).lower():
                args.pop("children_ages")
                found = await self.mcp.call("search_hotels", args)
            else:
                raise
        rows = (found or {}).get("hotels") or []
        meta = (found or {}).get("meta") or {}
        resolved = (meta.get("resolved_geo") or {}).get("name")
        if not rows:
            return HotelResult([], "full", note="Источник не нашёл отелей под эти условия", resolved_name=resolved)

        top = rows[: q.limit_hotels]
        now = datetime.now(timezone.utc)

        async def details(row: dict) -> list[Offer]:
            d_args = {"product_type": "hotels", "hotel_id": str(row["hotel_id"]), "check_in": args["check_in"],
                      "check_out": args["check_out"], "adults": q.adults, "view": "compact"}
            if q.children_ages:
                d_args["children_ages"] = q.children_ages
            try:
                d = await self.mcp.call("get_offer_details", d_args)
            except SourceUnavailable:
                raise
            except McpError as e:
                log.warning("details failed for %s: %s", row.get("hotel_id"), e)
                return []
            if str(d.get("hotel", {}).get("hotel_id")) != str(row["hotel_id"]):
                log.warning("details echo mismatch for %s", row.get("hotel_id"))
                return []
            return normalize_rates(d, now, max_rooms=4 if (q.bed_type or q.view) else 2, need_breakfast=q.breakfast,
                                   need_cancel=q.free_cancellation, bed_type=q.bed_type, view=q.view)

        parts = await asyncio.gather(*(details(r) for r in top))
        offers = [o for p in parts for o in p]
        coverage = "partial" if meta.get("has_more") or len(rows) > len(top) else "full"
        return HotelResult(offers, coverage, resolved_name=resolved)


    async def reviews(self, hotel_id: str, q: HotelQuery) -> list[Review]:
        """The newest guest reviews of one hotel (one extra call, only for the hotel that was chosen)."""
        d = await self.mcp.call("get_offer_details", {
            "product_type": "hotels", "hotel_id": str(hotel_id), "check_in": q.check_in.isoformat(),
            "check_out": q.check_out.isoformat(), "adults": q.adults, "view": "reviews", "review_limit": 12})
        return normalize_reviews(d)

    async def reprice(self, offer: Offer, q: HotelQuery) -> Offer | None:
        """Re-fetch one offer from the source (same room/meal/cancellation id). None if it is gone."""
        d = await self.mcp.call("get_offer_details", {
            "product_type": "hotels", "hotel_id": offer.hotel.provider_hotel_id if offer.hotel else "",
            "check_in": q.check_in.isoformat(), "check_out": q.check_out.isoformat(), "adults": q.adults, "view": "compact",
            **({"children_ages": q.children_ages} if q.children_ages else {})})
        rate = offer.rate
        fresh = normalize_rates(d, datetime.now(timezone.utc), max_rooms=100,
                                need_breakfast=bool(rate and rate.breakfast_included),
                                need_cancel=bool(rate and rate.free_cancellation))
        return next((o for o in fresh if o.id == offer.id), None)


# --------------------------------------------------------------------------- demo data

_HOTEL_NAMES = ["Центральная", "Парк-Отель", "Старый город", "Гранд Плаза", "Уют", "Панорама", "Резиденция", "Вокзальная"]


class MockHotels:
    """Deterministic synthetic hotels around the city centre. Every offer carries data_mode='mock'."""

    async def reviews(self, hotel_id: str, q: HotelQuery) -> list[Review]:
        return []  # no invented guest opinions: demo hotels simply have none

    async def search(self, q: HotelQuery) -> HotelResult:
        nights = max((q.check_out - q.check_in).days, 1)
        rng = random.Random(f"{q.city}|{q.check_in}|{q.check_out}")
        center = q.center
        now = datetime.now(timezone.utc)
        offers: list[Offer] = []
        for i, name in enumerate(rng.sample(_HOTEL_NAMES, 6)):
            stars = rng.choice([3, 3, 4, 4, 5])
            rating = round(rng.uniform(7.2, 9.4), 1)
            per_night = Decimal(int(rng.uniform(2800, 5200) * (0.8 + 0.25 * stars) / 100) * 100)
            breakfast = rng.random() < 0.7 or q.breakfast
            free_cancel = rng.random() < 0.75 or q.free_cancellation
            lat = (center.lat + rng.uniform(-0.04, 0.04)) if center and center.lat is not None else None
            lon = (center.lon + rng.uniform(-0.05, 0.05)) if center and center.lon is not None else None
            hotel = HotelInfo(provider_hotel_id=f"mock-{i}", name=f"Отель «{name}» (демо)", stars=stars, rating=rating,
                              review_count=rng.randint(80, 2400), address=f"{q.city}, демо-адрес {i + 1}",
                              lat=lat, lon=lon, photos=[], amenities=["Wi-Fi"], check_in_time="14:00", check_out_time="12:00",
                              front_desk_24h=rng.random() < 0.6)
            until = datetime.combine(q.check_in, datetime.min.time(), tzinfo=timezone.utc).replace(hour=9)
            offers.append(Offer(
                id=_stable_id("mock", q.city, i, q.check_in, q.check_out), kind="hotel_rate", source="mock", data_mode="mock",
                retrieved_at=now, price=Money(amount=per_night * nights, currency="RUB", basis="stay_total"), hotel=hotel,
                rate=HotelRate(room_name="Стандартный номер", bed_type="double", meal="breakfast" if breakfast else "none",
                               breakfast_included=breakfast, free_cancellation=free_cancel,
                               free_cancellation_until=until if free_cancel else None, max_occupancy=3),
                note="Демо-данные: отель и цена сгенерированы"))
        if q.breakfast:
            offers = [o for o in offers if o.rate and o.rate.breakfast_included]
        if q.free_cancellation:
            offers = [o for o in offers if o.rate and o.rate.free_cancellation]
        return HotelResult(offers, "full", note="Демо-данные", resolved_name=q.city)
