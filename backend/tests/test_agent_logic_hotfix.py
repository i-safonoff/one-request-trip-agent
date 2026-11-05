"""Defects found by live runs (real GigaChat + live Tutu): each test is one observed wrong answer."""
from types import SimpleNamespace

from agents.domain.scoring import _vs_title
from agents.integrations.mcp_client import SourceUnavailable

STAY_SPB = {"intent": "new_trip", "scope": "full", "stops": [{"name": "Питер"}],
            "date_from": "2026-11-14", "date_to": "2026-11-16"}


def script(planner=None, constraints=None, budget=None):
    return {"PlannerOut": planner or {}, "ConstraintsOut": constraints or {}, "BudgetOut": budget or {}}


async def test_whole_trip_without_origin_gets_stays_now_and_asks_origin_alongside(make_orch):
    """«Хочу в Казань на выходные, вдвоём» used to stop at «Откуда выезжаете?» with no plan at all."""
    orch, store, llm, hotels = make_orch(script(planner=STAY_SPB, constraints={"adults": 2}))
    res = await orch.handle_message("c1", "r1", "m1", "Хочу в Питер на выходные, вдвоём", [])
    assert res.turn.kind == "plan" and hotels.queries  # the stay was searched without waiting for the origin
    assert res.state.nodes["stay_spb"].selected_offer_ref
    assert [q["field"] for q in res.turn.questions] == ["origin"] and "Откуда выезжаете?" in res.turn.text

    llm.script.update(script(planner={"intent": "other", "origin": "Москва"}))
    res = await orch.handle_message("c1", "r2", "m2", "Из Москвы", [])
    s = res.state
    assert s.sequence[0] == "outbound" and s.sequence[-1] == "return"  # the answer adds the tickets
    assert all(q.status == "answered" for q in s.questions) and not res.turn.questions


async def test_tickets_only_still_wait_for_the_origin(make_orch):
    orch, *_ = make_orch(script(planner={**STAY_SPB, "scope": "transport"}, constraints={"adults": 1}))
    res = await orch.handle_message("c2", "r1", "m1", "Билеты в Питер на 14 ноября", [])
    assert res.waiting_user and res.turn.kind == "question"


async def test_unreachable_source_is_not_reported_as_nothing_fits(make_orch):
    """Seen live: «Подходящих вариантов не нашлось … «stay_spb» … (ConnectTimeout: )»."""
    async def down(q):
        raise SourceUnavailable("ConnectTimeout: ")
    orch, store, llm, hotels = make_orch(script(planner={**STAY_SPB, "scope": "stay"}, constraints={"adults": 2}))
    hotels.search = down
    res = await orch.handle_message("c3", "r1", "m1", "Отель в Питере на выходные", [])
    text = res.turn.text
    assert "не нашлось" not in text and "не ответил" in text
    assert "Проживание: Питер" in text or "Проживание: Санкт-Петербург" in text  # a place, not a node id
    for leak in ("stay_spb", "ConnectTimeout", "Не удалось:"):
        assert leak not in text


async def test_a_silent_understanding_agent_gives_a_plain_warning_not_an_exception(make_orch):
    orch, store, llm, _ = make_orch(script(planner={**STAY_SPB, "scope": "stay"}, constraints={"adults": 2}))
    await orch.handle_message("c4", "r1", "m1", "Отель в Питере на выходные", [])
    llm.script = {"BudgetOut": {"amount": "20000", "scope": "stay"}}  # planner and constraints agents fail
    res = await orch.handle_message("c4", "r2", "m2", "до 20 тысяч", [])
    assert "Часть запроса разобрать не удалось" in res.turn.text
    assert "FakeLLM" not in res.turn.text and "planner:" not in res.turn.text


def _offer(hotel, room):
    return SimpleNamespace(hotel=SimpleNamespace(name=hotel), rate=SimpleNamespace(room_name=room),
                           transport=None, title=lambda: f"{hotel}, {room}")


def test_another_room_of_the_same_hotel_is_named_by_the_room():
    """Seen live: «Старосадский … по сравнению с «Старосадский»: дешевле на 712 ₽»."""
    a, b = _offer("Старосадский", "Двухместный Эконом"), _offer("Старосадский", "Двухместный Стандарт")
    assert _vs_title(a, b) == "Двухместный Стандарт, этот же отель"
    assert _vs_title(a, _offer("Cosmos Smart", "Стандарт")) == "Cosmos Smart"
