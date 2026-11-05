"""Regressions for the defects found by the external audit of integration/merged (b2dd43f): cart safety, stale offers,
transport identity, repeated cities, dates vs nights, destination wording, source modes, view shape."""
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from agents.config import Settings
from agents.domain.apply import apply_ops
from agents.domain.budget import compute_pricing
from agents.domain.cartfp import cart_fingerprint
from agents.domain.checks import run_checks
from agents.domain.graph import search_sig
from agents.domain.models import Cart, TripState
from agents.domain.ops import (AddConstraint, SelectOffer, SetBudget, SetDates, SetOrigin, SetRoute, SetScope, StopIn)
from agents.domain.turn import unsupported_claims
from agents.integrations.transport import normalize_offer
from agents.runtime.db_store import DbPlanStore
from agents.runtime.destinations import DestinationAgent, wanted_tags, wants_warm
from agents.shop import service as shop
from shared.models import Order
from shared.plan_view import VIEW_DEFAULTS, with_defaults

from . import test_agents_flow as flow
from .test_agents_flow import HOTELS_MOSCOW, build, drain, guest, send
from .test_domain import hotel, rank, stay_state
from .test_transport import EmptyHotels, load, searcher, trip

client = flow.client  # the HTTP client fixture of the merged-system tests
NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)
BODY = {"guest": {"name": "Иван Иванов", "email": "ivan@example.com"}}


# ------------------------------------------------------------------ F01 / F02: the cart and the order
async def ready_cart(client, db):
    worker, _, _ = build(db, HOTELS_MOSCOW)
    h, chat = await guest(client)
    await send(client, h, chat, "Найди отели в Москве", "m1")
    await drain(worker)
    pid = (await client.get(f"/api/v1/chats/{chat}/plan", headers=h)).json()["plan_id"]
    cid = (await client.post(f"/api/v1/plans/{pid}/cart", headers=h)).json()["cart"]["id"]
    assert (await client.post(f"/api/v1/carts/{cid}/check", headers=h)).json()["ready_to_pay"] is True
    return h, chat, cid


async def test_a_paid_cart_is_final_the_same_order_comes_back_and_it_cannot_be_checked_again(client, db):
    h, chat, cid = await ready_cart(client, db)
    first = (await client.post(f"/api/v1/carts/{cid}/checkout", json=BODY, headers=h)).json()
    again = await client.post(f"/api/v1/carts/{cid}/checkout", json=BODY, headers=h)  # a double click / a retry
    assert again.status_code == 200 and again.json()["order_number"] == first["order_number"] and again.json()["already_paid"] is True
    assert (await client.post(f"/api/v1/carts/{cid}/check", headers=h)).status_code == 409  # no way back to "checked"
    async with db.sessionmaker() as s:
        assert len((await s.scalars(select(Order))).all()) == 1


async def test_a_checked_cart_cannot_be_paid_after_the_plan_moved(client, db):
    h, chat, cid = await ready_cart(client, db)
    store = DbPlanStore(db)
    state = await store.load(chat)
    moved = apply_ops(state, "planner", [SetDates(shift_days=2)]).state  # same thing the agents do on "на два дня позже"
    await store.save(chat, None, moved, [], [], "сдвиг дат")
    r = await client.post(f"/api/v1/carts/{cid}/checkout", json=BODY, headers=h)
    assert r.status_code == 409
    async with db.sessionmaker() as s:
        assert (await s.scalars(select(Order))).all() == []


def test_the_cart_dies_with_the_plan_it_was_built_for():
    s = rank(stay_state(hotel(1, 10000)))
    cart = shop.build_cart(s)
    s = shop.commit_cart(s, cart, "in_cart")
    assert s.cart.fingerprint == cart_fingerprint(s, s.cart.items) and s.cart.status == "draft"
    changed = apply_ops(s, "planner", [SetDates(shift_days=2)]).state
    assert changed.cart == Cart() and changed.status != "in_cart"  # rebuilt and re-checked from scratch
    harmless = apply_ops(s, "constraints", [AddConstraint(kind="pets_allowed")]).state
    assert harmless.cart.id == cart.id  # a change that touches nothing in the cart leaves it alone


def test_a_stale_node_blocks_the_cart():
    s = rank(stay_state(hotel(1, 10000)))
    s = apply_ops(s, "planner", [SetDates(shift_days=2)]).state
    assert s.nodes["stay_spb"].status == "stale" and s.nodes["stay_spb"].selected_offer_ref
    try:
        shop.build_cart(s)
        raise AssertionError("a cart was built from a stale node")
    except shop.CartError as e:
        assert "без выбранного варианта" in e.message


# ------------------------------------------------------------------ F04: stale offers are not a current price
def test_old_offers_on_new_dates_do_not_make_a_ready_plan():
    s = rank(stay_state(hotel(1, 10000)))
    assert compute_pricing(s)["is_complete"] is True
    s = apply_ops(s, "planner", [SetDates(shift_days=2)]).state  # new dates, the re-search failed: the node stays stale
    p = compute_pricing(s)
    assert p["is_complete"] is False and p["total"] is None and p["missing"] == ["stay_spb"]
    err = [c for c in run_checks(s, NOW) if c.code == "NODE_UNSEARCHED"]
    assert err and err[0].severity == "error"


async def test_failed_research_after_a_date_shift_leaves_the_plan_partial(make_orch):
    script = {"PlannerOut": {"intent": "new_trip", "scope": "stay", "stops": [{"name": "Москва"}], "date_from": "2026-11-06", "date_to": "2026-11-09"},
              "ConstraintsOut": {"adults": 2}, "BudgetOut": {}}
    orch, store, llm, hotels = make_orch(script)
    r1 = await orch.handle_message("c1", "r1", "m1", "отели в Москве", [])
    assert r1.state.status == "ready"

    async def down(q):
        from agents.integrations.mcp_client import SourceUnavailable
        raise SourceUnavailable("timeout")
    hotels.search = down
    llm.script.update({"PlannerOut": {"intent": "modify", "shift_days": 2}, "ConstraintsOut": {}, "BudgetOut": {}})
    r2 = await orch.handle_message("c1", "r2", "m2", "на два дня позже", [])
    assert r2.state.status == "partial" and r2.state.nodes["stay_msk"].status == "stale"
    assert compute_pricing(r2.state)["is_complete"] is False


# ------------------------------------------------------------------ F03: the same trip at a new price
def test_transport_offer_keeps_its_id_when_only_the_price_changes():
    raw = load("tutu_search_rail_msk_spb.json")["offers"][0]
    a = normalize_offer(raw, "rail", 2, NOW, {})
    raw["price"]["amount"] += 500
    raw["price_party"]["amount"] += 1000
    b = normalize_offer(raw, "rail", 2, NOW, {})
    assert a.id == b.id and b.price.amount == a.price.amount + 1000  # a price change is a change, not a vanished train


async def test_reprice_finds_the_dearer_fare_even_if_it_dropped_out_of_the_short_list():
    from agents.integrations.transport import TransportQuery, TutuTransport
    from agents.domain.geo import place_from_name
    data = load("tutu_search_rail_msk_spb.json")

    class Mcp:
        async def call(self, name, args):
            return data
    tr = TutuTransport(Mcp())
    q = TransportQuery(place_from_name("Москва")[1], place_from_name("Питер")[1], date(2026, 11, 14), adults=2)
    offers = await tr._one("rail", q, everything=True)
    target = offers[-1]
    for c in data["offers"]:  # every card gets dearer; the target is no longer among the cheapest
        c["price"]["amount"] += 1000
        c["price_party"]["amount"] += 2000
    found = await tr.reprice(target, q)
    assert found is not None and found.id == target.id and round(found.price.amount - target.price.amount) == 2000


# ------------------------------------------------------------------ F05 / F06: graph and dates
def test_a_city_visited_twice_gets_two_stays():
    s = apply_ops(TripState(), "planner", [SetScope(scope="stay"), SetRoute(stops=[
        StopIn(name="Москва", nights=1), StopIn(name="Санкт-Петербург", nights=2), StopIn(name="Москва", nights=1)]),
        SetDates(date_from=date(2026, 11, 1), date_to=date(2026, 11, 5))]).state
    assert s.sequence == ["stay_msk", "stay_spb", "stay_msk_2"] and len(set(s.sequence)) == 3
    a, c = s.nodes["stay_msk"], s.nodes["stay_msk_2"]
    assert (a.check_in, a.check_out) == (date(2026, 11, 1), date(2026, 11, 2)) and (c.check_in, c.check_out) == (date(2026, 11, 4), date(2026, 11, 5))


def test_the_return_to_a_city_also_gets_its_own_transport_nodes():
    s = apply_ops(TripState(), "planner", [SetScope(scope="full"), SetOrigin(name="Казань"), SetRoute(stops=[
        StopIn(name="Москва", nights=1), StopIn(name="Санкт-Петербург", nights=1), StopIn(name="Москва", nights=1)]),
        SetDates(date_from=date(2026, 11, 1), date_to=date(2026, 11, 4))]).state
    assert len(s.sequence) == len(set(s.sequence))


def test_a_new_end_date_beats_the_nights_remembered_from_before():
    s = apply_ops(TripState(), "planner", [SetScope(scope="stay"), SetRoute(stops=[StopIn(name="Москва", nights=3)]),
                                           SetDates(date_from=date(2026, 11, 1), date_to=date(2026, 11, 4))]).state
    s = apply_ops(s, "planner", [SetDates(date_to=date(2026, 11, 6))]).state
    n = s.nodes["stay_msk"]
    assert (n.check_in, n.check_out) == (date(2026, 11, 1), date(2026, 11, 6))
    assert not [c for c in run_checks(s, NOW) if c.code == "TRIP_DATES_MISMATCH"]


def test_several_stops_the_last_one_absorbs_a_changed_end_date_and_it_is_said_so():
    s = apply_ops(TripState(), "planner", [SetScope(scope="stay"), SetRoute(stops=[StopIn(name="Москва", nights=2), StopIn(name="Казань", nights=2)]),
                                           SetDates(date_from=date(2026, 11, 1), date_to=date(2026, 11, 5))]).state
    s = apply_ops(s, "planner", [SetDates(date_to=date(2026, 11, 8))]).state
    assert s.nodes["stay_kzn"].check_out == date(2026, 11, 8) and [x.nights for x in s.route] == [2, 5]
    assert any(d.scope == "nights" for d in s.data_notes)


def test_the_verifier_catches_stays_that_do_not_cover_the_requested_period():
    s = apply_ops(TripState(), "planner", [SetScope(scope="stay"), SetRoute(stops=[StopIn(name="Москва", nights=3)]),
                                           SetDates(date_from=date(2026, 11, 1), date_to=date(2026, 11, 4))]).state
    s.date_to = date(2026, 11, 6)  # a state that disagrees with itself
    assert [c.severity for c in run_checks(s, NOW) if c.code == "TRIP_DATES_MISMATCH"] == ["error"]


# ------------------------------------------------------------------ F07: «тёплое море» in every spelling
def test_warm_sea_wishes_are_understood_in_any_wording():
    for text in ("Хочу куда-нибудь к тёплому морю в ноябре", "хочу к теплому морю", "отдых у моря", "на море", "поехать к морю", "море"):
        assert "море" in wanted_tags(text), text
    assert wants_warm("к тёплому морю") and wants_warm("теплое море") and wants_warm("где жарко")
    assert wanted_tags("мороженое и морковь") == set() and "лыжи" in wanted_tags("покататься на лыжах")


def test_the_travel_time_limit_is_never_lifted_to_get_a_longer_list():
    agent = DestinationAgent(None, None, Settings())
    s = apply_ops(TripState(), "planner", [SetOrigin(name="Москва")]).state
    s = apply_ops(s, "constraints", [AddConstraint(kind="max_travel_hours", value="0.5", strength="hard")]).state
    s.destination_text = "куда-нибудь к тёплому морю"
    assert agent.shortlist(s) == []  # nothing within 30 minutes: an honest empty list, not the whole catalogue
    s = apply_ops(s, "constraints", [AddConstraint(kind="max_travel_hours", value="4", strength="hard")]).state
    short = agent.shortlist(s)
    assert short and all(d["hours_from_msk"] <= 4 and "море" in d["tags"] for d in short)


# ------------------------------------------------------------------ F09: the `tutu` mode never invents data
async def test_tutu_mode_keeps_an_empty_answer_empty():
    s = trip(city="Токио")
    sr = searcher(EmptyHotels(), type("T", (), {"search": staticmethod(lambda q: _empty())})(), "tutu")
    stay, notes = await sr.search_stay(s, s.nodes["stay_tokyo"])
    assert stay.offers == [] and not any(n.kind == "mock" for n in notes)
    tr, notes = await sr.search_transport(s, s.nodes["outbound"])
    assert tr.offers == [] and not any(n.kind == "mock" for n in notes)


async def _empty():
    return []


# ------------------------------------------------------------------ F10: filter VALUES and a higher budget make a new search
def test_changing_the_value_of_a_search_filter_makes_the_node_stale():
    s = rank(stay_state(hotel(1, 10000)))
    s = apply_ops(s, "constraints", [AddConstraint(kind="view", value="sea", strength="soft")]).state
    s = apply_ops(s, "orchestrator", [SelectOffer(node_id="stay_spb", offer_id="h1")]).state
    s.nodes["stay_spb"].status, s.nodes["stay_spb"].search_sig = "selected", search_sig(s, s.nodes["stay_spb"])
    again = apply_ops(s, "constraints", [AddConstraint(kind="view", value="city", strength="soft")]).state
    assert again.nodes["stay_spb"].status == "stale"


def test_a_higher_price_ceiling_needs_a_new_search_a_lower_one_only_a_rerank():
    s = rank(stay_state(hotel(1, 10000)))
    s = apply_ops(s, "budget", [SetBudget(amount=Decimal(40000), scope="stay")]).state
    s.nodes["stay_spb"].status, s.nodes["stay_spb"].search_sig = "selected", search_sig(s, s.nodes["stay_spb"])
    assert apply_ops(s, "budget", [SetBudget(amount=Decimal(20000), scope="stay")]).state.nodes["stay_spb"].status == "selected"
    assert apply_ops(s, "budget", [SetBudget(amount=Decimal(60000), scope="stay")]).state.nodes["stay_spb"].status == "stale"


# ------------------------------------------------------------------ F08: a plan written by someone else still has the full view shape
def test_a_map_only_view_is_completed_for_the_web_ui():
    v = with_defaults({"title": "Как в «Япония»", "nodes": {"stay_x": {"id": "stay_x", "type": "stay"}}, "sequence": ["stay_x"]})
    assert set(VIEW_DEFAULTS) <= set(v) and v["pricing"]["within_budget"] is None and v["checks"] == []
    assert v["nodes"]["stay_x"]["label"] == "Проживание" and v["nodes"]["stay_x"]["options"] == []
    from agents.domain.view import build_view
    assert set(VIEW_DEFAULTS) <= set(build_view(TripState()))  # the defaults stay in step with the real view


# ------------------------------------------------------------------ F12 / F17
def test_an_answer_naming_a_hotel_or_promising_what_the_data_do_not_say_is_caught():
    facts = "Cosmos Smart Москва Дубининская. Завтрак включён"
    assert unsupported_claims("Рекомендую «Cosmos Smart Москва Дубининская» с завтраком", facts) == []
    bad = unsupported_claims("Рекомендую отель «Несуществующий Суперлюкс»: вам гарантирован бесплатный трансфер", facts)
    assert any("Несуществующий" in b for b in bad) and any("гарантир" in b for b in bad)


async def test_a_hijacked_answer_with_an_invented_hotel_falls_back_to_the_template(make_orch):
    script = {"PlannerOut": {"intent": "new_trip", "scope": "stay", "stops": [{"name": "Москва"}], "date_from": "2026-11-06", "date_to": "2026-11-09"},
              "ConstraintsOut": {"adults": 2}, "BudgetOut": {},
              "AnswerOut": {"text": "Рекомендую отель «Несуществующий Суперлюкс»: вам гарантирован бесплатный трансфер и размещение с собакой."}}
    orch, *_ = make_orch(script)
    res = await orch.handle_message("c2", "r1", "m1", "Найди отели в Москве", [])
    assert "Суперлюкс" not in res.turn.text and "Cosmos Smart" in res.turn.text
