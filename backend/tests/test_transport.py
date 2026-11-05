from pathlib import Path
"""Tutu transport on real recorded responses, and the auto / tutu / mock source modes."""
import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from agents.runtime.search import NodeSearchFailed, Searcher
from agents.config import Settings
from agents.domain.apply import apply_ops
from agents.domain.geo import place_from_name
from agents.domain.models import TripState
from agents.domain.ops import SetDates, SetOrigin, SetParty, SetRoute, SetScope, StopIn
from agents.integrations.hotels import HotelResult
from agents.integrations.mcp_client import McpError, SourceUnavailable
from agents.integrations.transport import TutuTransport, TransportQuery, modes_for, normalize_offer
FIX = Path(__file__).parent / "fixtures"

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


def load(name):
    return json.loads((FIX / name).read_text("utf-8"))


def test_rail_price_is_the_party_price_not_multiplied_again():
    o = normalize_offer(load("tutu_search_rail_msk_spb.json")["offers"][0], "rail", 2, NOW, {})
    assert o.price.amount == Decimal("2686.54")  # `price_party` for 2 passengers; the seat price is 1343.27
    t = o.transport
    assert t.mode == "rail" and t.duration_min == 320 and t.transfers == 0
    assert t.segments[0].origin_name == "Москва — Ленинградский вокзал"  # station code stripped for people
    assert t.segments[0].departure.utcoffset().total_seconds() == 3 * 3600  # time zone kept
    assert "купе" in t.fare_conditions and o.data_mode == "live" and o.source == "tutu_mcp"


def test_rail_price_falls_back_to_seat_price_times_party_when_no_party_price():
    raw = load("tutu_search_rail_msk_spb.json")["offers"][0]
    raw.pop("price_party")
    assert normalize_offer(raw, "rail", 3, NOW, {}).price.amount == Decimal("1343.27") * 3


def test_avia_price_already_covers_the_party_and_fare_conditions_are_read():
    o = normalize_offer(load("tutu_search_avia_msk_spb.json")["offers"][1], "flight", 2, NOW, {})
    assert o.price.amount == Decimal("7826.0")  # NOT multiplied by 2
    assert o.transport.segments[0].number == "S7-1015" and "Домодедово" in o.transport.segments[0].origin_name
    assert "без багажа" in o.transport.baggage and "невозвратный" in o.transport.baggage


def test_hub_coordinates_attach_when_known_and_stay_none_otherwise():
    raw = load("tutu_search_avia_msk_spb.json")["offers"][0]
    seg = raw["legs"][0]["segments"][0]
    o = normalize_offer(raw, "flight", 2, NOW, {seg["to"]: (59.80, 30.26)})
    assert o.transport.segments[0].destination_lat == 59.80 and o.transport.segments[0].origin_lat is None


def test_which_tutu_products_serve_a_pair():
    msk, spb, tokyo, osaka = (place_from_name(n)[1] for n in ("Москва", "Питер", "Токио", "Осака"))
    assert modes_for(msk, spb) == ["rail", "flight"]          # 630 km: train and plane, bus is too long
    assert modes_for(msk, tokyo) == ["flight"] and modes_for(tokyo, osaka) == ["flight"]  # Tutu rail/bus are Russian


class FakeMcp:
    def __init__(self, responses=None, error=None):
        self.responses, self.error, self.calls = responses or {}, error, []

    async def call(self, name, args):
        self.calls.append((name, args))
        if self.error:
            raise self.error
        return self.responses[name]


async def test_tutu_transport_searches_the_right_tools_with_the_right_party_arguments():
    mcp = FakeMcp({"search_rail": load("tutu_search_rail_msk_spb.json"), "search_avia": load("tutu_search_avia_msk_spb.json")})
    q = TransportQuery(place_from_name("Москва")[1], place_from_name("Питер")[1], date(2026, 11, 14), adults=2, children=1)
    offers = await TutuTransport(mcp).search(q)
    called = dict(mcp.calls)
    assert called["search_rail"]["passengers"] == 3 and called["search_rail"]["departure_date"] == "2026-11-14"
    assert called["search_avia"]["adults"] == 2 and called["search_avia"]["children"] == 1
    assert {o.transport.mode for o in offers} == {"rail", "flight"} and all(o.data_mode == "live" for o in offers)


async def test_one_failing_product_does_not_hide_the_others():
    class Half(FakeMcp):
        async def call(self, name, args):
            if name == "search_rail":
                raise McpError("could not resolve origin")
            return await super().call(name, args)
    mcp = Half({"search_avia": load("tutu_search_avia_msk_spb.json")})
    q = TransportQuery(place_from_name("Москва")[1], place_from_name("Питер")[1], date(2026, 11, 14), adults=2)
    assert {o.transport.mode for o in await TutuTransport(mcp).search(q)} == {"flight"}


# ---------------------------------------------------------------- source modes in the searcher
class DownHotels:
    async def search(self, q):
        raise SourceUnavailable("handshake timed out")


class EmptyHotels:
    async def search(self, q):
        return HotelResult([], "full")


class DownTransport:
    async def search(self, q):
        raise SourceUnavailable("handshake timed out")


def trip(origin="Москва", city="Санкт-Петербург"):
    s = apply_ops(TripState(), "planner", [SetScope(scope="full"), SetOrigin(name=origin), SetRoute(stops=[StopIn(name=city)]),
                                          SetDates(date_from=date(2026, 11, 14), date_to=date(2026, 11, 16))]).state
    return apply_ops(s, "constraints", [SetParty(adults=2)]).state


def searcher(hotels, transport, source):
    return Searcher(hotels, transport, Settings(hotels_source=source, transport_source=source, nominatim_enabled=False))


async def test_auto_falls_back_to_labelled_demo_data_when_the_source_is_unreachable():
    s = trip()
    sr = searcher(DownHotels(), DownTransport(), "auto")
    stay, notes = await sr.search_stay(s, s.nodes["stay_spb"])
    assert stay.offers and all(o.data_mode == "mock" for o in stay.offers)
    assert any(n.kind == "mock" and "недоступен" in n.text for n in notes)
    tr, notes = await sr.search_transport(s, s.nodes["outbound"])
    assert tr.offers and all(o.data_mode == "mock" for o in tr.offers)
    assert any(n.kind == "mock" and "недоступен" in n.text for n in notes)


async def test_tutu_mode_reports_the_failure_instead_of_inventing_data():
    s = trip()
    sr = searcher(DownHotels(), DownTransport(), "tutu")
    with pytest.raises(NodeSearchFailed) as e:
        await sr.search_stay(s, s.nodes["stay_spb"])
    assert e.value.unavailable
    with pytest.raises(NodeSearchFailed):
        await sr.search_transport(s, s.nodes["outbound"])


async def test_empty_answer_for_a_foreign_destination_gets_labelled_demo_hotels():
    s = trip(city="Токио")
    stay, notes = await searcher(EmptyHotels(), DownTransport(), "auto").search_stay(s, s.nodes["stay_tokyo"])
    assert stay.offers and all(o.data_mode == "mock" for o in stay.offers) and any(n.kind == "mock" for n in notes)
