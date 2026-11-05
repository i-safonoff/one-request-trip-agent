"""The merged system end to end: guest API -> chat service -> agent_runs queue -> agents worker -> plan, events, cart.

The worker runs in-process on the same in-memory database; the LLM is scripted, hotel data are recorded Tutu
responses, transport is the labelled demo generator.
"""
import uuid

import httpx
import pytest
from sqlalchemy import select

from agents.config import Settings as AgentSettings
from agents.integrations.llm import FakeLLM, LLMError
from agents.integrations.transport import MockTransport
from agents.runtime.db_store import DbPlanStore
from agents.runtime.destinations import DestinationAgent
from agents.runtime.orchestrator import Orchestrator
from agents.runtime.search import Searcher
from agents.runtime.worker import Worker
from main import app
from shared.events import events_after
from shared.models import (AgentRun, Clarification, InheritedItem, Order, PlanOperation, TripPlanVersion)
from trip_map.schema import Trip

from .conftest import FixtureHotels

HOTELS_MOSCOW = {"PlannerOut": {"intent": "new_trip", "scope": "stay", "stops": [{"name": "Москва"}],
                                "date_from": "2026-11-06", "date_to": "2026-11-09"},
                 "ConstraintsOut": {"adults": 2, "add": [{"kind": "breakfast"}, {"kind": "free_cancellation"}]},
                 "BudgetOut": {"amount": "30000", "scope": "stay"}}


def build(db, script, fail=False):
    settings = AgentSettings(hotels_source="tutu", transport_source="mock", nominatim_enabled=False)
    llm = FakeLLM(script)
    hotels = FixtureHotels()
    store = DbPlanStore(db)

    def make():
        if fail:
            raise LLMError("GIGACHAT_AUTH_KEY is not set")
        return Orchestrator(llm, Searcher(hotels, MockTransport(), settings), DestinationAgent(llm, hotels, settings), store, settings)
    app.state.hotels, app.state.transport = hotels, MockTransport()
    return Worker(db, make, store, stale_seconds=60, poll=0.01), llm, hotels


async def drain(worker):
    """Process every run that can run right now, oldest first."""
    n = 0
    while (rid := await worker.claim()):
        await worker.process(rid)
        n += 1
    return n


@pytest.fixture
async def client(db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def guest(client):
    r = (await client.post("/api/v1/auth/guest")).json()
    h = {"Authorization": f"Bearer {r['token']}"}
    chat = (await client.get("/api/v1/chats/active", headers=h)).json()["id"]
    return h, chat


async def send(client, h, chat, text, cid=None):
    r = await client.post(f"/api/v1/chats/{chat}/messages", json={"text": text, "client_message_id": cid or str(uuid.uuid4())}, headers=h)
    assert r.status_code == 202, r.text
    return r.json()


async def test_guest_message_becomes_a_plan_with_the_agents_answer(client, db):
    worker, llm, hotels = build(db, HOTELS_MOSCOW)
    h, chat = await guest(client)
    await send(client, h, chat, "Найди отели в Москве на 6-9 ноября", "m1")
    assert (await client.get(f"/api/v1/chats/{chat}/status", headers=h)).json()["busy"] is True
    assert await drain(worker) == 1

    msgs = (await client.get(f"/api/v1/chats/{chat}/messages", headers=h)).json()
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[0]["status"] == "done"
    turn = msgs[1]["turn"]
    assert turn["kind"] == "plan" and turn["recommendations"][0]["why"] and "Cosmos Smart" in msgs[1]["text"]
    assert msgs[1]["plan_id"] and msgs[1]["plan_revision"] >= 1

    plan = (await client.get(f"/api/v1/chats/{chat}/plan", headers=h)).json()
    assert plan["status"] == "ready" and plan["view"]["sequence"] == ["stay_msk"] and plan["view"]["pricing"]["is_complete"]
    assert (await client.get(f"/api/v1/chats/{chat}/status", headers=h)).json()["busy"] is False

    async with db.sessionmaker() as s:
        types = [e.type.value for e in await events_after(s, uuid.UUID(chat))]
        versions = (await s.scalars(select(TripPlanVersion).order_by(TripPlanVersion.revision))).all()
        ops = (await s.scalars(select(PlanOperation))).all()
    assert types[0] == "request.queued" and types[-1] == "assistant.message" and "plan.updated" in types and "plan.ready" in types
    assert len(versions) >= 3 and [v.revision for v in versions] == sorted(v.revision for v in versions)  # immutable snapshots
    assert any(o.status == "applied" and o.actor == "constraints" for o in ops)  # the journal says who changed what


async def test_a_question_is_a_clarification_that_does_not_hold_the_queue(client, db):
    worker, llm, _ = build(db, {"PlannerOut": {"intent": "new_trip", "scope": "full", "stops": [{"name": "Питер"}],
                                               "date_from": "2026-11-14", "date_to": "2026-11-16"},
                                "ConstraintsOut": {}, "BudgetOut": {}})
    h, chat = await guest(client)
    await send(client, h, chat, "Хочу в Питер на выходные", "m1")
    await drain(worker)
    status = (await client.get(f"/api/v1/chats/{chat}/status", headers=h)).json()
    q = status["open_question"]
    assert q and "Откуда" in q["question"] and status["busy"] is False  # waiting for the user, queue is free
    async with db.sessionmaker() as s:
        assert (await s.scalar(select(AgentRun.status))) == "waiting_clarification"
        assert (await s.scalar(select(Clarification.field))) == "origin,party"
    msgs = (await client.get(f"/api/v1/chats/{chat}/messages", headers=h)).json()
    assert msgs[-1]["turn"]["questions"][0]["id"] == q["id"]  # the UI answers THIS clarification id

    # the answer, through the chat module's own endpoint
    llm.script.update({"PlannerOut": {"intent": "other", "origin": "Москва"}, "ConstraintsOut": {"adults": 2}})
    r = await client.post(f"/api/v1/chats/{chat}/clarifications/{q['id']}/answers", json={"answer": "Из Москвы, нас двое"}, headers=h)
    assert r.status_code == 202
    await drain(worker)
    plan = (await client.get(f"/api/v1/chats/{chat}/plan", headers=h)).json()["view"]
    assert plan["sequence"][0] == "outbound" and "stay_spb" in plan["sequence"] and plan["status"] in ("ready", "partial")
    async with db.sessionmaker() as s:
        assert {r.status for r in await s.scalars(select(AgentRun))} == {"done"}  # the waiting run was superseded
        assert (await s.scalar(select(Clarification.status))) == "answered"


async def test_cart_check_and_simulated_payment(client, db):
    worker, _, _ = build(db, HOTELS_MOSCOW)
    h, chat = await guest(client)
    await send(client, h, chat, "Найди отели в Москве", "m1")
    await drain(worker)
    plan = (await client.get(f"/api/v1/chats/{chat}/plan", headers=h)).json()
    pid = plan["plan_id"]

    cart = await client.post(f"/api/v1/plans/{pid}/cart", headers=h)
    assert cart.status_code == 201
    cid = cart.json()["cart"]["id"]
    body = {"guest": {"name": "Иван Иванов", "email": "ivan@example.com"}}
    assert (await client.post(f"/api/v1/carts/{cid}/checkout", json=body, headers=h)).status_code == 409  # not re-checked yet
    chk = (await client.post(f"/api/v1/carts/{cid}/check", headers=h)).json()
    assert chk["ready_to_pay"] is True, chk
    paid = await client.post(f"/api/v1/carts/{cid}/checkout", json=body, headers=h)
    assert paid.status_code == 200 and paid.json()["order_number"].startswith("EKV-")
    final = (await client.get(f"/api/v1/chats/{chat}/plan", headers=h)).json()["view"]
    assert final["status"] == "booked" and final["cart"]["status"] == "paid"
    async with db.sessionmaker() as s:
        assert (await s.scalar(select(Order.status))) == "paid_mock"
        types = [e.type.value for e in await events_after(s, uuid.UUID(chat))]
    assert "order.paid" in types


async def test_ui_actions_use_the_same_queue_and_are_owner_only(client, db):
    worker, _, _ = build(db, HOTELS_MOSCOW)
    h, chat = await guest(client)
    await send(client, h, chat, "Найди отели в Москве", "m1")
    await drain(worker)
    plan = (await client.get(f"/api/v1/chats/{chat}/plan", headers=h)).json()
    node = plan["view"]["nodes"]["stay_msk"]
    alt = next(o["offer_id"] for o in node["options"] if o["offer_id"] != node["selected_offer_ref"] and o["passes_hard"])

    stranger, _ = await guest(client)
    r = await client.post(f"/api/v1/plans/{plan['plan_id']}/nodes/stay_msk/select", json={"offer_id": alt}, headers=stranger)
    assert r.status_code == 404  # other people's plans look exactly like missing ones

    r = await client.post(f"/api/v1/plans/{plan['plan_id']}/nodes/stay_msk/select", json={"offer_id": alt, "pin": True}, headers=h)
    assert r.status_code == 202
    assert await drain(worker) == 1
    after = (await client.get(f"/api/v1/chats/{chat}/plan", headers=h)).json()["view"]["nodes"]["stay_msk"]
    assert after["selected_offer_ref"] == alt and after["pinned"] is True


async def test_runs_of_one_chat_are_strictly_sequential(client, db):
    worker, _, _ = build(db, HOTELS_MOSCOW)
    h, chat = await guest(client)
    for i in range(3):
        await send(client, h, chat, f"сообщение {i}", f"m{i}")
    first = await worker.claim()
    assert first is not None and await worker.claim() is None  # the chat already has a running run
    await worker.process(first)
    second = await worker.claim()
    assert second is not None and second != first


async def test_llm_failure_is_reported_to_the_user_not_lost(client, db):
    worker, _, _ = build(db, HOTELS_MOSCOW, fail=True)
    h, chat = await guest(client)
    await send(client, h, chat, "привет", "m1")
    await drain(worker)
    msgs = (await client.get(f"/api/v1/chats/{chat}/messages", headers=h)).json()
    assert msgs[0]["status"] == "failed" and msgs[1]["turn"]["kind"] == "error" and "GIGACHAT_AUTH_KEY" in msgs[1]["text"]
    async with db.sessionmaker() as s:
        types = [e.type.value for e in await events_after(s, uuid.UUID(chat))]
    assert "request.failed" in types


async def test_accepted_history_items_are_applied_but_never_override_the_user(client, db):
    script = {**HOTELS_MOSCOW, "ConstraintsOut": {"adults": 2, "add": [{"kind": "breakfast", "strength": "hard"}]}}
    worker, _, _ = build(db, script)
    h, chat = await guest(client)
    async with db.sessionmaker() as s:
        from shared.models import Chat
        chat_row = await s.get(Chat, uuid.UUID(chat))
        for key, item in (("constraint:breakfast", {"type": "constraint", "kind": "breakfast", "value": True, "strength": "soft"}),
                          ("constraint:pets_allowed", {"type": "constraint", "kind": "pets_allowed", "value": True, "strength": "soft"}),
                          ("priority:rating", {"type": "priority", "key": "rating", "weight": 2.0}),
                          ("hotel:like:cosmos", {"type": "hotel", "polarity": "like", "name": "Cosmos Smart Москва Дубининская"})):
            s.add(InheritedItem(chat_id=chat_row.id, user_id=chat_row.user_id, item_key=key, item=item, reason="в прошлой поездке понравилось",
                                confidence=0.9, status="accepted"))
        await s.commit()
    await send(client, h, chat, "Найди отели в Москве", "m1")
    await drain(worker)
    state = await DbPlanStore(db).load(chat)
    kinds = {c.kind: c for c in state.constraints}
    assert kinds["breakfast"].strength == "hard" and kinds["breakfast"].source.origin == "user"  # THIS trip's words win
    assert kinds["pets_allowed"].strength == "soft" and kinds["pets_allowed"].source.origin == "history"  # inherited, soft
    assert any(p.key == "rating" for p in state.priorities) and len(state.inherited_applied) == 4
    assert state.history_hints and state.history_hints[0]["polarity"] == "like"
    sel = state.offers[state.nodes["stay_msk"].selected_offer_ref]
    assert any("прошлой поездке" in w for w in (await client.get(f"/api/v1/chats/{chat}/messages", headers=h)).json()[-1]["turn"]["recommendations"][0]["why"])
    assert sel.hotel.name.startswith("Cosmos")


async def test_the_plan_view_is_a_valid_map_plan_and_the_map_can_read_it(client, db):
    worker, _, _ = build(db, {"PlannerOut": {"intent": "new_trip", "scope": "full", "stops": [{"name": "Питер"}], "origin": "Москва",
                                             "date_from": "2026-11-14", "date_to": "2026-11-16"},
                              "ConstraintsOut": {"adults": 2}, "BudgetOut": {}})
    h, chat = await guest(client)
    await send(client, h, chat, "Питер из Москвы на выходные, двое", "m1")
    await drain(worker)
    plan = (await client.get(f"/api/v1/chats/{chat}/plan", headers=h)).json()
    trip = Trip.model_validate(plan["view"])  # the map module's own schema accepts the agents' view
    assert {"outbound", "stay_spb", "return"} <= set(trip.nodes)
    r = await client.get(f"/trip-map/api/trip?plan_id={plan['plan_id']}", headers=h)
    assert r.status_code == 200 and len(r.json()["steps"]) == len(plan["view"]["sequence"])
