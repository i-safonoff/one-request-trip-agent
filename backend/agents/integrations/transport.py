"""Transport options between two places.

Two providers:
- `TutuTransport`: live offers from the Tutu MCP (`search_rail`, `search_avia`, `search_bus`), written
  against real responses (tests/fixtures/tutu_search_*.json). Tutu returns station/airport NAMES, not
  coordinates, so hubs are geocoded best-effort (needed for transfers and the map).
- `MockTransport`: deterministic generator; EVERY offer is labelled data_mode='mock'. Used when Tutu is
  unreachable (`TRANSPORT_SOURCE=auto`), has nothing for a route (e.g. Japan), or when asked for explicitly.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from ..domain.geo import haversine_km
from ..domain.models import Money, Offer, Place, Segment, TransportInfo
from .mcp_client import McpClient, SourceUnavailable

log = logging.getLogger("transport")


@dataclass
class TransportQuery:
    origin: Place
    destination: Place
    day: date
    adults: int
    children: int = 0


class TransportProvider(Protocol):
    async def search(self, q: TransportQuery) -> list[Offer]: ...


def _hub(p: Place, kind: str) -> tuple[str, float, float]:
    """Airport / station next to the city centre (airports sit ~25 km out, stations in the centre)."""
    name = p.name or "?"
    if kind == "flight":
        return f"Аэропорт {name}", (p.lat or 0) + 0.18, (p.lon or 0) + 0.22
    return f"{name}, вокзал", (p.lat or 0) + 0.005, (p.lon or 0) + 0.005


class MockTransport:
    async def search(self, q: TransportQuery) -> list[Offer]:
        o, d = q.origin, q.destination
        if o.lat is None or d.lat is None:
            return []
        dist = haversine_km(o.lat, o.lon, d.lat, d.lon)
        same_country = bool(o.country and o.country == d.country)
        rng = random.Random(f"{o.name}|{d.name}|{q.day}")
        pax = Decimal(str(q.adults + 0.75 * q.children))
        specs: list[tuple[str, time, int, int, str, int]] = []  # mode, dep, minutes, price/pax, carrier, transfers
        if dist > 350 or not same_country:
            direct = int(dist / 750 * 60 + 45 + 40)
            base = 2500 + dist * 4.6 + (6000 if not same_country else 0)
            specs += [("flight", time(7, 10), direct, int(base * 1.10), "Авиакомпания (демо)", 0),
                      ("flight", time(13, 40), direct, int(base * 1.25), "Авиакомпания (демо)", 0),
                      ("flight", time(19, 25), int(direct * 1.7), int(base * 0.82), "Авиакомпания (демо)", 1)]
        if same_country and dist < 2000:
            speed = 110 if dist < 800 else 75
            specs.append(("rail", time(8, 30), int(dist / speed * 60 * 1.12 + 20), int(700 + dist * 2.4), "Скорый поезд (демо)", 0))
            if dist > 450:
                specs.append(("rail", time(22, 40), int(dist / 70 * 60 + 40), int(600 + dist * 1.7), "Ночной поезд (демо)", 0))
        if same_country and dist < 600:
            specs.append(("bus", time(9, 0), int(dist / 55 * 60 + 25), int(400 + dist * 1.5), "Автобус (демо)", 0))
        offers: list[Offer] = []
        for i, (mode, dep_t, minutes, per_pax, carrier, transfers) in enumerate(specs):
            jitter = Decimal(str(round(rng.uniform(0.92, 1.08), 3)))
            price = (Decimal(per_pax) * pax * jitter).quantize(Decimal("1")) // 10 * 10
            dep = datetime.combine(q.day, dep_t, tzinfo=ZoneInfo(o.tz or "UTC"))
            arr = (dep + timedelta(minutes=minutes)).astimezone(ZoneInfo(d.tz or "UTC"))
            on, olat, olon = _hub(o, mode)
            dn, dlat, dlon = _hub(d, mode)
            if transfers:
                mid = dep + timedelta(minutes=minutes // 2)
                segs = [Segment(mode=mode, carrier=carrier, origin_name=on, origin_lat=olat, origin_lon=olon,
                                destination_name="Пересадка (демо)", departure=dep, arrival=mid),
                        Segment(mode=mode, carrier=carrier, origin_name="Пересадка (демо)",
                                destination_name=dn, destination_lat=dlat, destination_lon=dlon,
                                departure=mid + timedelta(minutes=95), arrival=arr + timedelta(minutes=95))]
                minutes += 95
                arr = arr + timedelta(minutes=95)
            else:
                segs = [Segment(mode=mode, carrier=carrier, number=f"{rng.randint(100, 999)}", origin_name=on,
                                origin_lat=olat, origin_lon=olon, destination_name=dn, destination_lat=dlat,
                                destination_lon=dlon, departure=dep, arrival=arr)]
            offers.append(Offer(
                id="mock_tr_" + hashlib.sha1(f"{o.name}|{d.name}|{q.day}|{i}".encode()).hexdigest()[:10],
                kind="transport", source="mock", data_mode="mock", retrieved_at=datetime.now(ZoneInfo("UTC")),
                price=Money(amount=price, currency="RUB", basis="party_total"),
                transport=TransportInfo(mode=mode, segments=segs, transfers=transfers, duration_min=minutes,
                                        overnight=(arr.date() != dep.astimezone(ZoneInfo(d.tz or "UTC")).date() and minutes > 300 and mode == "rail"),
                                        baggage="1 место" if mode == "flight" else None),
                note="Демо-данные: расписание и цена сгенерированы"))
        return offers


# ------------------------------------------------------------------ Tutu (live)

_HUB_CODE = re.compile(r"\s*\(\d+\)\s*$")  # "Ленинградский вокзал (2006004)" -> station code is not for people
TOOL = {"rail": "search_rail", "flight": "search_avia", "bus": "search_bus"}


def modes_for(o: Place, d: Place) -> list[str]:
    """Which Tutu products can serve the pair. Tutu rail/bus are Russian; avia is worldwide."""
    if o.lat is None or d.lat is None:
        return ["flight"]
    dist = haversine_km(o.lat, o.lon, d.lat, d.lon)
    domestic_ru = o.country == "Россия" and d.country == "Россия"
    modes = []
    if domestic_ru and dist < 2200:
        modes.append("rail")
    if dist > 350 or not domestic_ru:
        modes.append("flight")
    if domestic_ru and dist < 600:
        modes.append("bus")
    return modes


def _hub_label(raw: str) -> str:
    return _HUB_CODE.sub("", raw).strip()


def _hub_query(raw: str, mode: str, city_hint: str = "") -> str:
    """What to ask the geocoder for a hub. "Москва — Казанский вокзал (2000003)" carries its city; a bus station
    ("Автовокзал Котельники", "Автовокзал Центральный (Щёлковский)") does not: the city of the search is added."""
    city, sep, hub = raw.partition(" — ")
    if not sep:
        city, hub = "", raw
    hub = re.sub(r"\(.*?\)|,\s*терм.*", "", hub).strip() or hub
    city = city.strip() or city_hint
    if mode == "flight" and city and hub != city:
        return f"аэропорт {hub}, {city}"
    return f"{hub}, {city}" if city and city.lower() not in hub.lower() else hub


def _hub_fallback(mode: str, city: str) -> str | None:
    """A station the geocoder does not know by its trade name ("Казань Пасс"): the main station of the city."""
    kind = {"rail": "железнодорожный вокзал", "bus": "автовокзал", "flight": "аэропорт"}.get(mode)
    return f"{kind} {city}" if kind and city else None


def _stable_id(*parts) -> str:
    return "tutu_tr_" + hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:10]


def normalize_offer(raw: dict, mode: str, party: int, retrieved_at: datetime, hubs: dict[str, tuple[float, float] | None]) -> Offer | None:
    """One Tutu transport card -> Offer (outbound leg only). `hubs` maps a hub name to coordinates, if known."""
    legs = raw.get("legs") or []
    if not legs or not legs[0].get("segments"):
        return None
    segs = []
    for sg in legs[0]["segments"]:
        a, b = hubs.get(sg["from"]), hubs.get(sg["to"])
        segs.append(Segment(
            mode=mode, carrier=sg.get("carrier"), number=sg.get("voyage_no"),
            origin_name=_hub_label(sg["from"]), origin_lat=a[0] if a else None, origin_lon=a[1] if a else None,
            destination_name=_hub_label(sg["to"]), destination_lat=b[0] if b else None, destination_lon=b[1] if b else None,
            departure=datetime.fromisoformat(sg["departure_at"]), arrival=datetime.fromisoformat(sg["arrival_at"])))
    price = raw["price"]
    note = None
    if mode == "rail":
        # rail prices are PER SEAT; `price_party` (present for 2+ passengers) is the cheapest fare x party
        amount = Decimal(str((raw.get("price_party") or {}).get("amount") or Decimal(str(price["amount"])) * party))
        note = "Цена за всех пассажиров «от»: считается, что все едут на одном (самом дешёвом) тарифе"
    else:
        amount = Decimal(str(price["amount"]))  # avia / bus already cover the whole party
    fare = None
    if mode == "rail" and raw.get("fares"):
        f = raw["fares"]
        cats = ", ".join({"SEDENTARY": "сидячий", "RESERVED_SEAT": "плацкарт", "COMPARTMENT": "купе", "LUX": "СВ"}.get(k, k)
                         for k in (f.get("seat_categories") or {}))
        fare = f"вагоны: {cats}" if cats else None
    elif raw.get("variants"):
        c = raw["variants"][0].get("conditions", {})
        bag = c.get("baggage") or {}
        bags = "без багажа" if not bag.get("pieces") else f"багаж {bag.get('kg') or '?'} кг"
        fare = f"{c.get('fare_family', 'тариф')}: {bags}, {'возвратный' if c.get('refundable') else 'невозвратный'}"
    dep, arr = segs[0].departure, segs[-1].arrival
    return Offer(
        id=_stable_id(mode, *(s.number for s in segs), dep.isoformat()),  # no price: a changed price is the SAME trip
        kind="transport", source="tutu_mcp", data_mode="live", retrieved_at=retrieved_at,
        price=Money(amount=amount, currency=price.get("currency", "RUB"), basis="party_total"),
        transport=TransportInfo(mode=mode, segments=segs, transfers=len(segs) - 1, duration_min=_door_to_door(raw, dep, arr),
                                overnight=mode == "rail" and arr.astimezone(dep.tzinfo).date() != dep.date(),
                                baggage=fare if mode == "flight" else None, fare_conditions=fare),
        raw_ref=f"tutu:{raw.get('transport')}:{raw.get('offer_id')}", note=note)



def _door_to_door(raw: dict, dep: datetime, arr: datetime) -> int:
    """Minutes from the first departure to the last arrival, waiting between legs included. The source's `duration_min` is the
    sum of the legs (a 75-hour trip with two long layovers was shown as 17 hours)."""
    given = int(raw.get("duration_min") or 0)
    try:
        elapsed = int((arr - dep).total_seconds() // 60) if dep.tzinfo and arr.tzinfo else 0
    except (TypeError, ValueError):
        elapsed = 0
    return max(given, elapsed)


class TutuTransport:
    def __init__(self, client: McpClient, geocode=None):
        self.mcp, self.geocode = client, geocode
        self._hubs: dict[str, tuple[float, float] | None] = {}

    def _args(self, mode: str, q: TransportQuery, sort: str = "price_asc", page: int = 1, page_size: int = 10) -> dict:
        base = {"origin": q.origin.name, "destination": q.destination.name, "departure_date": q.day.isoformat(),
                "page_size": page_size, "sort": sort, "view": "compact", **({"page": page} if page > 1 else {})}
        if mode == "rail":
            return {**base, "passengers": max(q.adults + q.children, 1)}
        return {**base, "adults": q.adults, "children": q.children}

    async def _geocode_hubs(self, raws: list[tuple[str, str, str]]) -> None:
        """raws: (hub name, mode, city the search is about). A hub the geocoder cannot place falls back to the city's main
        station; failing that it stays unknown and the map draws the city-to-city line."""
        if not self.geocode:
            return
        for raw, mode, city in raws:
            if raw in self._hubs and self._hubs[raw] is not None:
                continue
            found = await asyncio.to_thread(self.geocode, _hub_query(raw, mode, city))
            if found is None and (fb := _hub_fallback(mode, raw.partition(" — ")[0].strip() if " — " in raw else city)):
                found = await asyncio.to_thread(self.geocode, fb)
            self._hubs[raw] = found

    async def _one(self, mode: str, q: TransportQuery, everything: bool = False, sort: str = "price_asc", page: int = 1,
                   page_size: int = 10) -> list[Offer]:
        res = await self.mcp.call(TOOL[mode], self._args(mode, q, sort, page, page_size))
        cards = (res or {}).get("offers") or []
        if not everything:  # a short, varied list: the cheapest few plus the fastest few
            pick = {c["offer_id"]: c for c in sorted(cards, key=lambda c: c["price"]["amount"])[:3]}
            pick.update({c["offer_id"]: c for c in sorted(cards, key=lambda c: c.get("duration_min") or 1e9)[:2]})
            cards = list(pick.values())
        todo = []
        for c in cards:
            for lg in c.get("legs", [])[:1]:
                segs = lg.get("segments", [])
                for i, sg in enumerate(segs):
                    # the first station belongs to the origin city, the last to the destination; stops in between have no hint
                    todo.append((sg["from"], mode, q.origin.name if i == 0 else ""))
                    todo.append((sg["to"], mode, q.destination.name if i == len(segs) - 1 else ""))
        await self._geocode_hubs(todo)
        now = datetime.now(ZoneInfo("UTC"))
        party = max(q.adults + q.children, 1)
        out = [normalize_offer(c, mode, party, now, self._hubs) for c in cards]
        return [o for o in out if o]

    async def search(self, q: TransportQuery) -> list[Offer]:
        modes = modes_for(q.origin, q.destination)
        results = await asyncio.gather(*(self._one(m, q) for m in modes), return_exceptions=True)
        offers: list[Offer] = []
        errors = [r for r in results if isinstance(r, Exception)]
        for r in results:
            if not isinstance(r, Exception):
                offers += r
        if not offers and errors:
            unavailable = [e for e in errors if isinstance(e, SourceUnavailable)]
            raise unavailable[0] if unavailable else errors[0]
        for e in errors:
            log.info("one transport product failed for %s → %s: %s", q.origin.name, q.destination.name, e)
        return offers

    async def reprice(self, offer: Offer, q: TransportQuery) -> Offer | None:
        """Search again and find the same departure (same id, whatever its price now). None if it is no longer sold.
        The whole answer is looked at, not the short list of a first search: a fare that got dearer drops out of that."""
        mode = offer.transport.mode if offer.transport else "flight"
        # the chosen offer is not necessarily among the 10 cheapest (a long flight picked for speed or comfort): look through
        # more of the answer in the orders the search used, and stop as soon as the same departure is found
        for sort, page in (("price_asc", 1), ("price_asc", 2), ("price_asc", 3), ("duration_asc", 1), ("duration_asc", 2),
                           ("departure_asc", 1), ("departure_asc", 2), ("departure_asc", 3),
                           ("price_asc", 1), ("duration_asc", 1)):  # the last two: the source's answers vary, ask once more
            found = await self._one(mode, q, everything=True, sort=sort, page=page, page_size=30)
            for o in found:
                if o.id == offer.id:
                    return o
            if not found:  # no such page: the next pages of this order do not exist either
                continue
        return None
