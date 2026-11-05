import json
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest

from agents.domain.apply import apply_ops
from agents.domain.budget import choose_set, compute_pricing
from agents.domain.models import HotelInfo, HotelRate, Money, Offer, TripState
from agents.domain.ops import *  # noqa: F401,F403
from agents.domain.ops import (AddConstraint, AttachOptions, ExcludeOffer, SelectOffer, SetBudget, SetDates,
                            SetOrigin, SetParty, SetPriorities, SetRanking, SetRoute, SetScope, StopIn, PriorityIn)
from agents.domain.scoring import rank_node, tradeoffs
from agents.domain.turn import numbers_in, unsupported_numbers
from agents.integrations.llm import extract_json
from agents.integrations.mcp_client import McpClient, McpError, SourceUnavailable

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)


def hotel(i, price, rating=8.0, fc=True, bf=True):
    return Offer(id=f"h{i}", kind="hotel_rate", source="tutu_mcp", data_mode="live", retrieved_at=NOW,
                 price=Money(amount=Decimal(price)), hotel=HotelInfo(provider_hotel_id=str(i), name=f"H{i}", rating=rating, review_count=300, lat=59.93, lon=30.33),
                 rate=HotelRate(room_name="Std", breakfast_included=bf, free_cancellation=fc))


def stay_state(*offers, **ops_kw):
    s = TripState()
    s = apply_ops(s, "planner", [SetScope(scope="stay"), SetRoute(stops=[StopIn(name="Питер")]),
                                 SetDates(date_from=date(2026, 11, 14), date_to=date(2026, 11, 16))]).state
    s = apply_ops(s, "constraints", [SetParty(adults=2)]).state
    s = apply_ops(s, "hotels", [AttachOptions(node_id="stay_spb", offers=list(offers))]).state
    return s


def rank(s):
    n = s.nodes["stay_spb"]
    sc, best = rank_node(s, n)
    return apply_ops(s, "orchestrator", [SetRanking(node_id="stay_spb", scores=sc, selected=best)]).state


# ------------------------------------------------------------- operations
def test_actor_cannot_use_foreign_operations():
    r = apply_ops(TripState(), "constraints", [SetRoute(stops=[StopIn(name="Казань")]), SetParty(adults=2)])
    assert r.state.party.adults == 2 and not r.state.route
    assert "may not use 'set_route'" in r.rejected[0]["reason"]


def test_invalid_operation_is_rolled_back_without_losing_the_others():
    r = apply_ops(TripState(), "planner", [SetRoute(stops=[StopIn(name="Казань")]),
                                           SetDates(date_from=date(2026, 11, 10), date_to=date(2026, 11, 9))])
    assert len(r.applied) == 1 and len(r.rejected) == 1 and r.state.route and r.state.date_to is None


def test_stale_revision_conflicts_only_with_overlapping_changes():
    s = TripState(revision=5)
    r = apply_ops(s, "constraints", [SetParty(adults=3), AddConstraint(kind="breakfast")], base_revision=3, newer_touched={"party"})
    assert [a["op"]["op"] for a in r.applied] == ["add_constraint"]  # party changed since revision 3: that op is stale
    assert "stale" in r.rejected[0]["reason"]


def test_changing_party_makes_priced_nodes_stale_but_budget_does_not():
    s = rank(stay_state(hotel(1, 10000), hotel(2, 12000)))
    assert apply_ops(s, "budget", [SetBudget(amount=Decimal(50000))]).state.nodes["stay_spb"].status == "selected"
    assert apply_ops(s, "constraints", [SetParty(add_adults=2)]).state.nodes["stay_spb"].status == "stale"


def test_search_filter_constraint_makes_node_stale_other_constraint_does_not():
    s = rank(stay_state(hotel(1, 10000)))
    assert apply_ops(s, "constraints", [AddConstraint(kind="breakfast")]).state.nodes["stay_spb"].status == "stale"
    assert apply_ops(s, "constraints", [AddConstraint(kind="pets_allowed")]).state.nodes["stay_spb"].status == "selected"


def test_pinned_offer_survives_a_new_search_and_exclude_unpins():
    s = rank(stay_state(hotel(1, 10000), hotel(2, 12000)))
    s = apply_ops(s, "user", [SelectOffer(node_id="stay_spb", offer_id="h2", pin=True)]).state
    s = apply_ops(s, "hotels", [AttachOptions(node_id="stay_spb", offers=[hotel(2, 12500), hotel(3, 9000)])]).state
    assert s.nodes["stay_spb"].selected_offer_ref == "h2" and s.nodes["stay_spb"].pinned
    s = apply_ops(s, "hotels", [AttachOptions(node_id="stay_spb", offers=[hotel(3, 9000)])]).state
    assert s.nodes["stay_spb"].selected_offer_ref is None and not s.nodes["stay_spb"].pinned  # pinned offer vanished
    s = rank(s)
    s = apply_ops(s, "planner", [ExcludeOffer(node_id="stay_spb")]).state
    assert s.nodes["stay_spb"].status == "stale" and s.nodes["stay_spb"].excluded_offers == ["h3"]


def test_graph_keeps_node_data_and_splits_nights_openly():
    s = TripState()
    s = apply_ops(s, "planner", [SetScope(scope="full"), SetOrigin(name="Москва"),
                                 SetRoute(stops=[StopIn(name="Казань"), StopIn(name="Нижний Новгород")]),
                                 SetDates(date_from=date(2026, 11, 1), date_to=date(2026, 11, 8))]).state
    assert s.sequence == ["outbound", "stay_kzn", "move_kzn__nnov", "stay_nnov", "return"]
    assert [(r.nights, r.nights_origin) for r in s.route] == [(4, "agent"), (3, "agent")]
    assert any(d.kind == "own_proposal" for d in s.data_notes)  # the split is the agent's own proposal: it says so
    s = apply_ops(s, "planner", [SetDates(shift_days=1)]).state
    assert s.nodes["stay_kzn"].check_in == date(2026, 11, 2) and s.nodes["return"].departure_date == date(2026, 11, 9)


# ------------------------------------------------------------- scoring / budget
def test_hard_constraint_beats_a_higher_score_and_unknown_is_not_a_pass():
    s = stay_state(hotel(1, 15000, 8.0, bf=True), hotel(2, 9000, 9.5, bf=False))
    s = apply_ops(s, "constraints", [AddConstraint(kind="breakfast")]).state
    sc, best = rank_node(s, s.nodes["stay_spb"])
    assert best == "h1" and sc["h2"].passes_hard is False
    unknown = hotel(3, 8000, 9.9)
    unknown.rate.breakfast_included = None
    s = apply_ops(s, "hotels", [AttachOptions(node_id="stay_spb", offers=[hotel(1, 15000), unknown])]).state
    sc, best = rank_node(s, s.nodes["stay_spb"])
    assert best == "h1"  # confirmed breakfast wins over "unknown"
    s = apply_ops(s, "hotels", [AttachOptions(node_id="stay_spb", offers=[unknown])]).state
    assert rank_node(s, s.nodes["stay_spb"])[1] == "h3"  # only unknowns left: allowed, but flagged by CONSTRAINT_UNKNOWN


def test_user_priorities_change_the_winner():
    cheap, rated = hotel(1, 9000, 7.0), hotel(2, 16000, 9.4)
    s = stay_state(cheap, rated)
    s_price = apply_ops(s, "constraints", [SetPriorities(items=[PriorityIn(key="price", weight=4)])]).state
    s_rating = apply_ops(s, "constraints", [SetPriorities(items=[PriorityIn(key="rating", weight=4)])]).state
    assert rank_node(s_price, s_price.nodes["stay_spb"])[1] == "h1"
    assert rank_node(s_rating, s_rating.nodes["stay_spb"])[1] == "h2"


def test_budget_picks_best_affordable_and_reports_infeasible():
    s = rank(stay_state(hotel(1, 10000, 7.0), hotel(2, 16000, 9.4)))
    s = apply_ops(s, "budget", [SetBudget(amount=Decimal(12000), scope="stay")]).state
    ch = choose_set(s)
    assert ch.selected["stay_spb"] == "h1" and ch.within is True
    s = apply_ops(s, "budget", [SetBudget(amount=Decimal(5000))]).state
    ch = choose_set(s)
    assert ch.within is False and ch.selected["stay_spb"] == "h1"  # cheapest, and it says the budget is not met
    p = compute_pricing(rank(apply_ops(s, "budget", [SetBudget(amount=Decimal(5000))]).state))
    assert p["within_budget"] is False


def test_tradeoffs_are_numeric_facts():
    s = rank(stay_state(hotel(1, 10000, 7.0), hotel(2, 16000, 9.4)))
    n = s.nodes["stay_spb"]
    t = tradeoffs(s, n)
    assert t and t[0]["deltas"][0]["criterion"] == "price"


def test_total_is_not_reported_as_complete_when_a_part_is_missing():
    s = TripState()
    s = apply_ops(s, "planner", [SetScope(scope="full"), SetOrigin(name="Москва"), SetRoute(stops=[StopIn(name="Питер")]),
                                 SetDates(date_from=date(2026, 11, 14), date_to=date(2026, 11, 16))]).state
    s = apply_ops(s, "hotels", [AttachOptions(node_id="stay_spb", offers=[hotel(1, 10000)])]).state
    s = rank(s)
    p = compute_pricing(s)
    assert p["is_complete"] is False and p["total"] is None and p["known_subtotal"] == "10000.00"
    assert set(p["missing"]) == {"outbound", "return"}


# ------------------------------------------------------------- text checks / parsing
def test_number_check_catches_invented_figures_and_accepts_formatting():
    facts = {"price": "17265.00", "dates": "14–16 ноября", "rating": 9.05, "start": "2026-11-14"}
    assert unsupported_numbers("Отель за 17 265 ₽, рейтинг 9,05, заезд 14 ноября", facts) == set()
    assert unsupported_numbers("Отель за 16 900 ₽", facts) == {"16900"}
    assert "17265" in numbers_in("17 265 ₽")


def test_extract_json_handles_fences_and_chatter():
    assert extract_json('Вот ответ:\n```json\n{"a": {"b": 1}}\n```') == {"a": {"b": 1}}
    assert extract_json('text {"x": "}"} tail') == {"x": "}"}
    with pytest.raises(ValueError):
        extract_json("нет json")


# ------------------------------------------------------------- MCP client
def _mcp(handler, retry_delays=()):
    return McpClient("http://mcp.test/mcp", transport=httpx.MockTransport(handler), retry_delays=retry_delays)


async def test_mcp_client_handshake_sse_and_whitelist():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body["method"])
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}},
                                  headers={"Mcp-Session-Id": "s1"})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": "search_hotels"}]}})
        assert req.headers["mcp-session-id"] == "s1"
        sse = 'event: message\ndata: ' + json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": '{"hotels": []}'}]}}) + "\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
    c = _mcp(handler)
    assert await c.call("search_hotels", {"city_name": "Москва"}) == {"hotels": []}
    assert calls == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    for forbidden in ("create_checkout_link", "register_checkout_passengers"):
        with pytest.raises(McpError, match="not allowed"):
            await c.call(forbidden, {})
    assert calls.count("tools/call") == 1  # forbidden tools never reach the server


async def test_mcp_client_distinguishes_unreachable_from_tool_error():
    def down(req):
        raise httpx.ConnectTimeout("handshake timed out")
    with pytest.raises(SourceUnavailable):
        await _mcp(down).call("search_hotels", {})

    def tool_error(req):
        body = json.loads(req.content)
        if body["method"] == "tools/call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"isError": True, "content": [{"type": "text", "text": "bad city"}]}})
        if body["method"] == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": {"protocolVersion": "2025-06-18"}})
    with pytest.raises(McpError, match="bad city"):
        await _mcp(tool_error).call("search_hotels", {})


async def test_mcp_client_retries_a_transient_timeout_but_not_a_tool_error():
    attempts = {"n": 0}

    def flaky(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body["method"] == "initialize":
            attempts["n"] += 1
            if attempts["n"] < 3:  # the first two connections time out
                raise httpx.ConnectTimeout("slow")
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": "{\"ok\": 1}"}]}})
    assert await _mcp(flaky, retry_delays=(0, 0)).call("search_hotels", {}) == {"ok": 1}
    assert attempts["n"] == 3  # two retries, then success


def test_unwrap_schema_echo():
    from agents.integrations.llm import unwrap_schema_echo
    from agents.runtime.schemas import PlannerOut
    raw = {"properties": {"intent": "new_trip", "origin": "Москва"}, "quote": "q"}
    out = PlannerOut.model_validate(unwrap_schema_echo(raw, PlannerOut))
    assert out.origin == "Москва" and out.quote == "q"
