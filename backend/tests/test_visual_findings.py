"""Defects found by the visual run (docs: fix plan "visual"), each pinned by a test."""
from decimal import Decimal
from datetime import datetime, timedelta, timezone

import pytest

from agents.domain.apply import apply_ops
from agents.domain.models import Constraint, Offer, Money, Place, Segment, TransportInfo, TripState
from agents.domain.ops import SetParty
from agents.domain.turn import cond_label
from agents.domain.view import _geometry
from agents.integrations.transport import _door_to_door
from agents.runtime.orchestrator import _INTERNAL_IDS, HELP_OPTIONS, _fix_party, _keep_scope, _total_party
from agents.runtime.schemas import ConstraintsOut, PlannerOut


def _trip(scope="full", adults=None, kids=()):
    s = TripState()
    s.scope = scope
    s.route = [type("R", (), {"place": "spb", "nights": None})()]
    s.party.adults = adults
    s.party.children_ages = list(kids)
    return s


# ---- V11 / V16: the headcount
def test_an_absolute_headcount_beats_an_increment():
    s = TripState()
    s.party.adults = 1
    res = apply_ops(s, "constraints", [SetParty(adults=4, add_adults=3)])
    assert res.state.party.adults == 4  # not 7
    res = apply_ops(s, "constraints", [SetParty(add_adults=2)])
    assert res.state.party.adults == 3


@pytest.mark.parametrize("text, total", [("нас будет четверо", 4), ("Нас стало 5", 5), ("нас двое", 2), ("едем вчетвером", 4),
                                          ("нас теперь 3", 3), ("6 человек", 6), ("сделай дешевле", None), ("до 5000 за всё", None)])
def test_total_party_is_read_from_the_words(text, total):
    assert _total_party(text) == total


def test_a_total_means_adults_after_the_children():
    cons, _ = _fix_party(ConstraintsOut(adults=4), None, "нас будет четверо", _trip(adults=3, kids=[3, 9]))
    assert cons.adults == 2  # 3 adults + 2 children and "четверо" -> 4 guests, not 6
    cons, _ = _fix_party(ConstraintsOut(adults=4, add_adults=3), None, "нас будет четверо", _trip(adults=1))
    assert cons.adults == 4 and cons.add_adults is None


def test_a_total_that_leaves_no_adult_is_asked():
    cons, planner = _fix_party(ConstraintsOut(adults=2), PlannerOut(), "нас двое", _trip(adults=1, kids=[3, 9]))
    assert cons.adults is None and planner.ambiguities and planner.ambiguities[0].field == "party"


def test_a_message_about_nobody_does_not_change_the_party():
    cons, _ = _fix_party(ConstraintsOut(adults=1, pets=["dog"]), None, "не знаю", _trip())
    assert cons.adults is None and cons.pets == ["dog"]
    same, _ = _fix_party(ConstraintsOut(add_adults=1), None, "добавь взрослого", _trip(adults=2))
    assert same.add_adults == 1


# ---- V15: the scope
def test_the_scope_is_not_switched_by_a_small_change():
    p = PlannerOut(scope="stay")
    assert _keep_scope(p, "убери завтрак", _trip("transport")).scope is None  # a day trip keeps its tickets
    assert _keep_scope(p, "убери завтрак", _trip("full")).scope is None
    assert _keep_scope(p, "только отель", _trip("transport")).scope == "stay"
    assert _keep_scope(PlannerOut(scope="full"), "добавь отель", _trip("transport")).scope == "full"


# ---- V01: geometry of a flight whose first airport has no coordinates
def _flight_state():
    s = TripState()
    s.places["dps"] = Place(name="Бали", lat=-8.65, lon=115.2)
    s.places["msk"] = Place(name="Москва", lat=55.75, lon=37.62)
    t0 = datetime(2026, 11, 13, 6, tzinfo=timezone.utc)
    segs = [Segment(mode="flight", origin_name="DPS", origin_lat=None, origin_lon=None, destination_name="HKT",
                    destination_lat=8.11, destination_lon=98.3, departure=t0, arrival=t0 + timedelta(hours=3)),
            Segment(mode="flight", origin_name="HKT", origin_lat=8.11, origin_lon=98.3, destination_name="SVO",
                    destination_lat=None, destination_lon=None, departure=t0 + timedelta(hours=8), arrival=t0 + timedelta(hours=17))]
    s.offers["o"] = Offer(id="o", kind="transport", source="mock", data_mode="mock", retrieved_at=t0,
                          price=Money(amount=1, currency="RUB", basis="party_total"),
                          transport=TransportInfo(mode="flight", segments=segs, transfers=1, duration_min=720))
    from agents.domain.models import Node
    n = Node(id="back", type="transport", from_="dps", to="msk", selected_offer_ref="o")
    s.nodes["back"] = n
    return s, n


def test_a_flight_starts_at_its_city_when_the_first_airport_is_unknown():
    s, n = _flight_state()
    geom, status = _geometry(s, n)
    assert status == "great_circle"
    assert geom["coordinates"] == [[115.2, -8.65], [98.3, 8.11], [37.62, 55.75]]  # Bali -> Phuket -> Moscow, not Phuket -> Moscow


# ---- V03: the door-to-door duration
def test_duration_counts_the_layovers():
    t0 = datetime(2026, 11, 13, 15, 5, tzinfo=timezone(timedelta(hours=8)))
    t1 = datetime(2026, 11, 16, 13, 30, tzinfo=timezone(timedelta(hours=3)))
    assert _door_to_door({"duration_min": 1020}, t0, t1) == 75 * 60 + 25  # 75 h 25 min, not 17 h
    assert _door_to_door({"duration_min": 600}, t0.replace(tzinfo=None), t1.replace(tzinfo=None)) == 600  # naive times: the source wins


# ---- V05: the user's words for conditions
def _c(kind, value=True, label=None):
    return Constraint(id=kind, kind=kind, value=value, label=label)


def test_conditions_are_named_in_russian():
    assert cond_label(_c("breakfast")) == "завтрак"
    assert cond_label(_c("free_cancellation")) == "бесплатная отмена"
    assert cond_label(_c("only_modes", ["rail"])) == "только поезд"
    assert cond_label(_c("exclude_modes", ["flight", "bus"])) == "без самолётов, автобусов"


@pytest.mark.parametrize("text, bad", [("Убрать условие free_cancellation", True), ("поезд tutu_tr_ab12", True), ("only_modes: rail", True),
                                       ("Только поезд, без самолётов", False)])
def test_internal_names_are_caught_in_an_answer(text, bad):
    assert bool(_INTERNAL_IDS.search(text)) is bad


# ---- V07: answer buttons from the first question
def test_origin_question_offers_buttons_including_a_hotel_only_answer():
    assert "Только отель, без билетов" in HELP_OPTIONS["origin"] and len(HELP_OPTIONS["origin"]) >= 3


def test_one_adult_is_never_a_default():
    cons, _ = _fix_party(ConstraintsOut(adults=1), None, "Хочу в Питер на выходные", _trip(adults=None))
    assert cons.adults is None
    keep, _ = _fix_party(ConstraintsOut(adults=1), None, "Еду один в Питер на выходные", _trip(adults=None))
    assert keep.adults == 1  # said in words
    keep, _ = _fix_party(ConstraintsOut(adults=1), None, "Хочу в Питер на выходные", _trip(adults=1))
    assert keep.adults == 1  # already known


# ---- "Только билеты на поезд" must restrict the search to trains
@pytest.mark.parametrize("text, kind, value", [
    ("Только билеты на поезд из Москвы в Казань", "only_modes", ["rail"]),
    ("Поездом в Нижний на выходные", "only_modes", ["rail"]),
    ("Хочу автобусом", "only_modes", ["bus"]),
    ("Без самолётов, пожалуйста", "exclude_modes", ["flight"]),
    ("не хочу на поезде", "exclude_modes", ["rail"])])
def test_a_named_transport_is_a_restriction(text, kind, value):
    from agents.runtime.orchestrator import _fix_modes
    cons = _fix_modes(ConstraintsOut(), text, TripState())
    assert [(c.kind, c.value) for c in cons.add] == [(kind, value)]


@pytest.mark.parametrize("text", ["Поездом или самолётом, как удобнее", "Билеты в Казань на 14 ноября", "Отель в Москве"])
def test_no_restriction_without_a_single_named_mode(text):
    from agents.runtime.orchestrator import _fix_modes
    assert _fix_modes(ConstraintsOut(), text, TripState()).add == []


# ---- stations: what the geocoder is asked for
def test_hub_queries_carry_the_city():
    from agents.integrations.transport import _hub_fallback, _hub_query
    assert _hub_query("Москва — Казанский вокзал (2000003)", "rail") == "Казанский вокзал, Москва"
    assert _hub_query("Автовокзал Котельники", "bus", "Москва") == "Автовокзал Котельники, Москва"
    assert _hub_query("Автовокзал Центральный (Щёлковский)", "bus", "Москва") == "Автовокзал Центральный, Москва"  # was "…, Автовокзал Центральный (Щёлковский)"
    assert _hub_query("Автовокзал Нижний Новгород", "bus", "Нижний Новгород") == "Автовокзал Нижний Новгород"
    assert _hub_query("Москва — Шереметьево (SVO), терм. D", "flight") == "аэропорт Шереметьево, Москва"
    assert _hub_fallback("rail", "Казань") == "железнодорожный вокзал Казань" and _hub_fallback("hotel", "Казань") is None


async def test_an_unknown_station_falls_back_to_the_citys_main_one():
    from agents.integrations.transport import TutuTransport
    asked = []

    def geocode(q):
        asked.append(q)
        return (55.79, 49.12) if q == "железнодорожный вокзал Казань" else None
    t = TutuTransport(None, geocode)
    await t._geocode_hubs([("Казань — Казань Пасс (2060500)", "rail", "Казань")])
    assert t._hubs["Казань — Казань Пасс (2060500)"] == (55.79, 49.12) and asked == ["Казань Пасс", "железнодорожный вокзал Казань"]


def test_a_derived_budget_does_not_block_the_purchase():
    from agents.domain.checks import run_checks
    from agents.domain.models import Source
    s, n = _flight_state()
    s.sequence = [n.id]
    s.budget.amount = Decimal("0.5")  # the trip costs more
    s.budget.source = Source(origin="user", quote="до 1 рубля")
    assert [c.severity for c in run_checks(s) if c.code == "BUDGET"] == ["error"]
    s.budget.source = Source(origin="derived", quote="сделай дешевле")
    assert [c.severity for c in run_checks(s) if c.code == "BUDGET"] == ["warning"]
