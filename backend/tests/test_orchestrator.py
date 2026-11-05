"""End-to-end orchestrator scenarios with a scripted LLM, recorded Tutu hotel data and demo transport."""
from datetime import date

from agents.integrations.llm import LLMError


def script(planner=None, constraints=None, budget=None, answer=None):
    s = {"PlannerOut": planner or {}, "ConstraintsOut": constraints or {}, "BudgetOut": budget or {}}
    if answer is not None:
        s["AnswerOut"] = answer
    return s


async def test_full_hotel_request_no_questions(make_orch):
    """Scenario 1 from the plan: a complete request is answered without any clarification."""
    orch, store, llm, hotels = make_orch(script(
        planner={"intent": "new_trip", "scope": "stay", "stops": [{"name": "Москва"}],
                 "date_from": "2026-11-06", "date_to": "2026-11-09"},
        constraints={"adults": 2, "add": [{"kind": "breakfast", "strength": "hard"},
                                          {"kind": "free_cancellation", "strength": "hard"}]},
        budget={"amount": "30000", "scope": "stay"}))
    res = await orch.handle_message("c1", "r1", "m1", "Найди отели в Москве на 6-9 ноября…", [])
    s = res.state
    assert res.turn.kind == "plan" and not res.waiting_user
    assert s.sequence == ["stay_msk"]  # only accommodation: no transport was asked for
    assert hotels.queries[0].breakfast and hotels.queries[0].free_cancellation
    assert hotels.queries[0].price_max_per_night == 10000  # per-night budget, as Tutu's contract wants
    node = s.nodes["stay_msk"]
    offer = s.offers[node.selected_offer_ref]
    assert offer.price.amount <= 30000 and offer.rate.breakfast_included and offer.rate.free_cancellation
    assert s.status == "ready" and not [c for c in s.checks if c.severity == "error"]
    assert "Cosmos Smart" in res.turn.text
    rec = res.turn.recommendations[0]
    assert rec.why and any("часть выдачи" in r for r in rec.risks)  # honest about partial coverage


async def test_incomplete_request_asks_one_batch_then_builds_trip(make_orch):
    orch, store, llm, _ = make_orch(script(planner={"intent": "new_trip", "scope": "full", "stops": [{"name": "Питер"}],
                                                    "date_from": "2026-11-14", "date_to": "2026-11-16"}))
    res = await orch.handle_message("c2", "r1", "m1", "Хочу в Питер на выходные", [])
    assert res.waiting_user and res.turn.kind == "question"
    assert {q["field"] for q in res.turn.questions} == {"origin", "party"}  # asked together, in ONE message
    assert res.turn.text.count("\n") >= 1

    llm.script.update(script(planner={"intent": "other", "origin": "Москва"}, constraints={"adults": 2}))
    res = await orch.handle_message("c2", "r2", "m2", "Из Москвы, нас двое", [])
    s = res.state
    assert not res.waiting_user and all(q.status == "answered" for q in s.questions)
    assert s.sequence[0] == "outbound" and "stay_spb" in s.sequence and s.sequence[-1] == "return"
    assert all(s.nodes[n].selected_offer_ref for n in ("outbound", "stay_spb", "return"))
    assert any(d.kind == "mock" and "демо" in d.text for d in s.data_notes)  # transport is honestly labelled


async def test_rebuild_keeps_untouched_parts(make_orch):
    orch, store, llm, hotels = make_orch(script(
        planner={"intent": "new_trip", "scope": "stay", "stops": [{"name": "Москва"}], "date_from": "2026-11-06", "date_to": "2026-11-09"},
        constraints={"adults": 2}))
    r1 = await orch.handle_message("c3", "r1", "m1", "отель в Москве", [])
    rev1 = r1.state.revision
    # a budget change only re-ranks: no new search
    llm.script.update(script(budget={"amount": "20000", "scope": "stay"}))
    n_queries = len(hotels.queries)
    r2 = await orch.handle_message("c3", "r2", "m2", "бюджет до 20 тысяч", [])
    assert len(hotels.queries) == n_queries and r2.state.revision > rev1
    # moving the dates makes the stay stale and searches again
    llm.script.update(script(planner={"intent": "modify", "shift_days": 2}, budget={}))
    r3 = await orch.handle_message("c3", "r3", "m3", "а если на два дня позже", [])
    assert len(hotels.queries) == n_queries + 1
    assert r3.state.date_from == date(2026, 11, 8) and r3.state.nodes["stay_msk"].check_in == date(2026, 11, 8)
    assert r3.state.budget.amount == 20000  # untouched condition survived the change


async def test_vague_destination_goes_through_destination_agent(make_orch, monkeypatch):
    from agents.integrations.weather import Climate
    import agents.runtime.destinations as dest

    async def fake_climate(client, lat, lon, sea, year, month):
        return Climate(air_c=24.0, sea_c=26.0 if sea else None, rain_mm_per_day=0.5, year=year)
    monkeypatch.setattr(dest, "month_climate", fake_climate)
    orch, store, llm, _ = make_orch(script(
        planner={"intent": "new_trip", "scope": "full", "destination_vague": "тёплое море", "origin": "Москва", "month_hint": "2026-11"},
        constraints={"adults": 2, "add_children_ages": [7], "add": [{"kind": "max_travel_hours", "value": 5}]},
        budget={"qualitative": "недорого"}))
    llm.script["DestinationsOut"] = {"picks": []}  # model gives nothing -> deterministic reasons from facts
    res = await orch.handle_message("c4", "r1", "m1", "куда-нибудь к тёплому морю в ноябре, недорого, с ребёнком 7 лет, без долгих перелётов", [])
    assert res.turn.kind == "destinations" and res.waiting_user
    cands = res.state.destination_candidates
    assert len(cands) == 3 and all(c.why for c in cands)
    assert all(c.facts["перелёт_из_Москвы_ч (справочно)"] <= 5 for c in cands)  # «без долгих перелётов» was applied
    assert res.state.status == "choosing_destination"


async def test_llm_failure_is_reported_not_swallowed(make_orch):
    orch, *_ = make_orch({})  # FakeLLM raises LLMError for every schema
    try:
        await orch.handle_message("c5", "r1", "m1", "привет", [])
        assert False, "must raise"
    except LLMError:
        pass


async def test_the_tracks_flagship_query_gets_destinations_before_any_question(make_orch, monkeypatch):
    """«Тёплое море в ноябре, недорого, с ребёнком, без долгих перелётов»: no origin, no headcount, no dates, and still
    the first answer is three explained destinations. Origin / dates / party are asked after the user picks one."""
    from agents.integrations.weather import Climate
    import agents.runtime.destinations as dest

    async def fake_climate(client, lat, lon, sea, year, month):
        return Climate(air_c=24.0, sea_c=26.0 if sea else None, rain_mm_per_day=0.5, year=year)
    monkeypatch.setattr(dest, "month_climate", fake_climate)
    orch, store, llm, _ = make_orch({
        "PlannerOut": {"intent": "new_trip", "scope": "full", "destination_vague": "тёплое море", "month_hint": "2026-11"},
        "ConstraintsOut": {"children_unknown": 1, "add": [{"kind": "max_travel_hours", "value": 5}]},
        "BudgetOut": {"qualitative": "недорого"}, "DestinationsOut": {"picks": []}})
    res = await orch.handle_message("c6", "r1", "m1", "куда-нибудь к тёплому морю в ноябре, недорого, с ребёнком, без долгих перелётов", [])
    assert res.turn.kind == "destinations" and len(res.state.destination_candidates) == 3 and not res.state.open_questions()

    res = await orch.handle_action("c6", "r2", {"type": "choose_destination", "key": res.state.destination_candidates[0].key})
    assert res.turn.kind == "question" and res.waiting_user
    assert {q["field"] for q in res.turn.questions} >= {"origin", "dates", "party"}  # now they are needed, and asked in ONE message
