"""Search step: fills nodes with options from the hotel and transport providers, plus geo enrichment
and the arrival/departure transfers between a hub (airport, station) and the hotel."""
from __future__ import annotations

import asyncio
import logging

from ..config import Settings
from ..domain.geo import estimate_tz, haversine_km, to_local
from ..domain.graph import stay_price_cap
from ..domain.models import DataNote, Node, TripState
from ..domain.hotel_fit import pick_feedback
from ..domain.ops import AttachOptions, SetGuestFeedback, SetPlaces, SetTransfers
from ..integrations.hotels import HotelProvider, HotelQuery, HotelResult, MockHotels
from ..integrations.mcp_client import McpError, SourceUnavailable
from ..integrations.transport import MockTransport, TransportProvider, TransportQuery

log = logging.getLogger("search")


class NodeSearchFailed(Exception):
    def __init__(self, node_id: str, reason: str, unavailable: bool = False):
        super().__init__(reason)
        self.node_id, self.reason, self.unavailable = node_id, reason, unavailable


def _pname(state: TripState, pid: str | None) -> str:
    p = state.places.get(pid or "")
    return (p.name if p else None) or pid or "?"


class Searcher:
    def __init__(self, hotels: HotelProvider, transport: TransportProvider, settings: Settings, geocoder=None):
        self.hotels, self.transport, self.settings, self.geocoder = hotels, transport, settings, geocoder
        self.mock_hotels = MockHotels()
        self.mock_transport = MockTransport()

    # ------------------------------------------------------------ geo
    async def enrich_places(self, state: TripState) -> SetPlaces | None:
        """Coordinates and time zone for places the catalog did not know (Nominatim, cached)."""
        items: dict[str, dict] = {}
        for pid, p in state.places.items():
            if p.lat is None and p.name and self.geocoder and self.settings.nominatim_enabled:
                coords = await asyncio.to_thread(self.geocoder, p.name)
                if coords:
                    tz, est = estimate_tz(*coords)
                    items[pid] = {"lat": coords[0], "lon": coords[1], "tz": tz, "tz_estimated": est}
            elif p.lat is not None and p.tz is None:
                tz, est = estimate_tz(p.lat, p.lon)
                items[pid] = {"tz": tz, "tz_estimated": est}
        return SetPlaces(items=items) if items else None

    # ------------------------------------------------------------ stay
    def _stay_query(self, state: TripState, node: Node) -> HotelQuery:
        hard = {c.kind for c in state.hard()}
        price_max = stay_price_cap(state, node)
        return HotelQuery(city=_pname(state, node.place), check_in=node.check_in, check_out=node.check_out,
                          adults=state.party.adults or 1, children_ages=list(state.party.children_ages),
                          breakfast="breakfast" in hard, free_cancellation="free_cancellation" in hard,
                          kid_friendly="kid_friendly" in hard, price_max_per_night=price_max,
                          bed_type=next((c.value for c in state.constraints if c.kind == "bed_type"), None),
                          view=next((c.value for c in state.constraints if c.kind == "view"), None),
                          center=state.places.get(node.place or ""))

    async def search_stay(self, state: TripState, node: Node) -> tuple[AttachOptions, list[DataNote]]:
        q = self._stay_query(state, node)
        notes: list[DataNote] = []
        country = (state.places.get(node.place or "").country if node.place in state.places else None)
        scope = f"search:{node.id}"
        result: HotelResult | None = None
        src = self.settings.hotels_source
        if src == "mock":
            result = await self.mock_hotels.search(q)
        else:
            try:
                result = await self.hotels.search(q)
            except (SourceUnavailable, McpError) as e:
                if src != "auto":
                    log.warning("hotel source failed for %s: %s", node.id, e)  # the user sees the reason, not the exception
                    raise NodeSearchFailed(node.id, "источник отелей сейчас не отвечает, повторите запрос через минуту"
                                           if isinstance(e, SourceUnavailable) else "источник отелей вернул ошибку",
                                           unavailable=isinstance(e, SourceUnavailable)) from e
                log.warning("hotel source failed (%s): falling back to demo data", e)
                result = await self.mock_hotels.search(q)
                notes.append(DataNote(scope=scope, kind="mock", text=f"Источник отелей недоступен: по «{q.city}» показаны демо-данные."))
            if src == "auto" and not result.offers and country not in (None, "Россия") and not any(n.kind == "mock" for n in notes):
                result = await self.mock_hotels.search(q)
                notes.append(DataNote(scope=scope, kind="mock",
                                      text=f"Источник не вернул отелей в направлении «{q.city}»: показаны демо-данные."))
        if result.coverage == "partial":
            notes.append(DataNote(scope=scope, kind="partial",
                                  text=f"{q.city}: просмотрена часть выдачи источника, не весь рынок."))
        if src == "mock":
            notes.append(DataNote(scope=scope, kind="mock", text="Отели: демо-данные (HOTELS_SOURCE=mock)."))
        return AttachOptions(node_id=node.id, offers=result.offers[:12], coverage=result.coverage), notes

    async def hotel_feedback(self, state: TripState, node: Node, offer) -> SetGuestFeedback | None:
        """Quotes from the newest reviews of the CHOSEN hotel that matter for this trip. Never fails the plan."""
        if offer.source != "tutu_mcp" or offer.hotel is None or offer.hotel.feedback_loaded:
            return None
        try:
            reviews = await self.hotels.reviews(offer.hotel.provider_hotel_id, self._stay_query(state, node))
        except (SourceUnavailable, McpError) as e:
            log.info("no reviews for %s: %s", offer.hotel.name, e)
            return None
        pros, cons, used = pick_feedback(state, reviews)
        return SetGuestFeedback(offer_id=offer.id, pros=pros, cons=cons, reviews_used=used)

    # ------------------------------------------------------------ transport
    async def search_transport(self, state: TripState, node: Node) -> tuple[AttachOptions, list[DataNote]]:
        o, d = state.places.get(node.from_ or ""), state.places.get(node.to or "")
        if not (o and d and o.lat is not None and d.lat is not None):
            raise NodeSearchFailed(node.id, "не удалось определить координаты городов маршрута")
        if not node.departure_date:
            raise NodeSearchFailed(node.id, "не определена дата отправления")
        query = TransportQuery(o, d, node.departure_date, state.party.adults or 1,
                               len(state.party.children_ages) + state.party.children_unknown)
        src = self.settings.transport_source
        label = f"Переезд {_pname(state, node.from_)} → {_pname(state, node.to)}"
        scope = f"search:{node.id}"
        notes: list[DataNote] = []
        offers = []
        if src == "mock":
            offers = await self.mock_transport.search(query)
        else:
            try:
                offers = await self.transport.search(query)
            except (SourceUnavailable, McpError) as e:
                if src != "auto":
                    log.warning("transport source failed for %s: %s", node.id, e)
                    raise NodeSearchFailed(node.id, "источник билетов сейчас не отвечает, повторите запрос через минуту"
                                           if isinstance(e, SourceUnavailable) else "источник билетов вернул ошибку",
                                           unavailable=isinstance(e, SourceUnavailable)) from e
                log.warning("transport source failed (%s): falling back to demo data", e)
                offers = await self.mock_transport.search(query)
                notes.append(DataNote(scope=scope, kind="mock", text=f"{label}: источник недоступен, показаны демо-данные."))
            if src == "auto" and not offers and not notes:  # in `tutu` mode an empty answer stays empty: no invented fares
                offers = await self.mock_transport.search(query)
                if offers:
                    notes.append(DataNote(scope=scope, kind="mock", text=f"{label}: источник не вернул вариантов, показаны демо-данные."))
        if src == "mock" and offers:
            notes.append(DataNote(scope=scope, kind="mock", text=f"{label}: демо-данные (TRANSPORT_SOURCE=mock): расписание и цены сгенерированы."))
        return AttachOptions(node_id=node.id, offers=self._arriving_in_time(state, node, offers), coverage="full"), notes

    @staticmethod
    def _arriving_in_time(state: TripState, node: Node, offers: list) -> list:
        """Before a stay: offers that arrive by the check-in day win over ones that land later (a night train would
        silently move the whole trip by a day). If nothing arrives in time (long flights), all offers stay."""
        stay = next((state.nodes[i] for i in state.sequence[state.sequence.index(node.id) + 1:]
                     if state.nodes[i].type != "transfer"), None) if node.id in state.sequence else None
        if not (stay and stay.type == "stay" and stay.check_in):
            return offers
        place = state.places.get(stay.place or "")
        in_time = [o for o in offers if o.transport and o.transport.segments
                   and to_local(o.transport.segments[-1].arrival, place.tz if place else None).date() <= stay.check_in]
        return in_time or offers


# ------------------------------------------------------------------ transfers

def _transfer_minutes(distance_km: float) -> int:
    return int(round(distance_km * 1.3 / 35 * 60 + 10))


def plan_transfers(state: TripState) -> tuple[SetPlaces | None, SetTransfers]:
    """Transfers between the hub where a transport arrives/leaves and the selected hotel.

    Only when the hub is >3 km from the hotel (airports yes, a station in the city centre usually no).
    Durations are straight-line estimates, flagged `estimated`.
    """
    places: dict[str, dict] = {}
    items: list[dict] = []
    seq = [state.nodes[i] for i in state.sequence if state.nodes[i].type != "transfer"]
    for i, n in enumerate(seq):
        if n.type != "transport" or not n.selected_offer_ref or n.selected_offer_ref not in state.offers:
            continue
        t = state.offers[n.selected_offer_ref].transport
        if not t:
            continue
        first, last = t.segments[0], t.segments[-1]
        nxt = seq[i + 1] if i + 1 < len(seq) else None
        prv = seq[i - 1] if i > 0 else None
        if nxt and nxt.type == "stay" and last.destination_lat is not None:
            h = state.offers.get(nxt.selected_offer_ref or "")
            if h and h.hotel and h.hotel.lat is not None:
                dist = haversine_km(last.destination_lat, last.destination_lon, h.hotel.lat, h.hotel.lon)
                if dist > 3:
                    hub = f"hub_{n.id}_arr"
                    places[hub] = {"name": last.destination_name, "lat": last.destination_lat, "lon": last.destination_lon,
                                   "parent": nxt.place, "kind": "airport" if t.mode == "flight" else "station",
                                   "tz": state.places[nxt.place].tz if nxt.place in state.places else None}
                    arrive_day = (last.arrival.date())
                    items.append({"id": f"transfer_in_{n.id}", "from_": hub, "to": nxt.place, "after": n.id,
                                  "direction": "after", "departure_date": arrive_day, "duration_min": _transfer_minutes(dist),
                                  "distance_km": round(dist, 1), "estimated": True})
        if prv and prv.type == "stay" and first.origin_lat is not None:
            h = state.offers.get(prv.selected_offer_ref or "")
            if h and h.hotel and h.hotel.lat is not None:
                dist = haversine_km(first.origin_lat, first.origin_lon, h.hotel.lat, h.hotel.lon)
                if dist > 3:
                    hub = f"hub_{n.id}_dep"
                    places[hub] = {"name": first.origin_name, "lat": first.origin_lat, "lon": first.origin_lon,
                                   "parent": prv.place, "kind": "airport" if t.mode == "flight" else "station",
                                   "tz": state.places[prv.place].tz if prv.place in state.places else None}
                    items.append({"id": f"transfer_out_{n.id}", "from_": prv.place, "to": hub, "after": n.id,
                                  "direction": "before", "departure_date": first.departure.date(),
                                  "duration_min": _transfer_minutes(dist), "distance_km": round(dist, 1), "estimated": True})
    return (SetPlaces(items=places) if places else None), SetTransfers(items=items)
