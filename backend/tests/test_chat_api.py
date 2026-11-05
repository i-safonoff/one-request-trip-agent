import hashlib
import hmac
import json
import time
import uuid
from urllib.parse import urlencode

import httpx
import pytest
from sqlalchemy import select

from chat.auth import InvalidInitData, issue_token, validate_init_data, verify_token
from chat.router import event_stream
from main import app
from shared.config import get_settings
from shared.events import AgentProgress, EventType, emit_event
from shared.models import AgentRun, Chat, TripPlan, TripPlanVersion, User

from .conftest import TEST_BOT_TOKEN


def make_init_data(user_id=42, *, token=TEST_BOT_TOKEN, auth_date=None, tamper=False) -> str:
    fields = {"auth_date": str(int(auth_date if auth_date is not None else time.time())), "query_id": "AAH",
              "user": json.dumps({"id": user_id, "first_name": "Ann", "username": "ann", "language_code": "ru"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if tamper:
        fields["user"] = fields["user"].replace(str(user_id), "7")
    return urlencode(fields)


# ---- initData and tokens ----

def test_valid_init_data_returns_the_user():
    user = validate_init_data(make_init_data(42), TEST_BOT_TOKEN, 3600)
    assert user["id"] == 42 and user["username"] == "ann"


@pytest.mark.parametrize("kwargs, why", [
    ({"tamper": True}, "signature"),
    ({"token": "999:OTHER"}, "signature"),
    ({"auth_date": 1}, "expired"),
])
def test_bad_init_data_is_rejected(kwargs, why):
    with pytest.raises(InvalidInitData, match=why):
        validate_init_data(make_init_data(**kwargs), TEST_BOT_TOKEN, 3600)


def test_init_data_without_hash_is_rejected():
    with pytest.raises(InvalidInitData):
        validate_init_data("auth_date=1&user=%7B%7D", TEST_BOT_TOKEN, 3600)


async def test_session_token_roundtrip_expiry_and_tampering(db):
    settings, uid = get_settings(), uuid.uuid4()
    token = issue_token(uid, settings)
    assert verify_token(token, settings) == uid
    assert verify_token(token, settings, now=time.time() + settings.session_ttl_seconds + 5) is None
    payload, sig = token.split(".")
    assert verify_token(payload + "." + sig[:-2] + "AA", settings) is None
    assert verify_token("garbage", settings) is None


# ---- HTTP API ----

@pytest.fixture
async def client(db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def login(client, user_id=42) -> dict:
    r = await client.post("/api/v1/auth/telegram", json={"init_data": make_init_data(user_id)})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def test_auth_rejects_forged_init_data(client):
    r = await client.post("/api/v1/auth/telegram", json={"init_data": make_init_data(tamper=True)})
    assert r.status_code == 401


async def test_endpoints_need_a_token(client):
    assert (await client.get("/api/v1/chats/active")).status_code == 401
    assert (await client.get("/api/v1/chats/active", headers={"Authorization": "Bearer x.y"})).status_code == 401


async def test_message_flow_and_idempotency(client, db):
    h = await login(client)
    chat = (await client.get("/api/v1/chats/active", headers=h)).json()
    url = f"/api/v1/chats/{chat['id']}/messages"
    r = await client.post(url, headers=h, json={"text": "Токио, 12-17 ноября", "client_message_id": "c1"})
    assert r.status_code == 202 and r.json()["duplicate"] is False and r.json()["run_id"]
    again = await client.post(url, headers=h, json={"text": "Токио, 12-17 ноября", "client_message_id": "c1"})
    assert again.json()["duplicate"] is True and again.json()["message_id"] == r.json()["message_id"]
    history = (await client.get(url, headers=h)).json()
    assert [m["text"] for m in history] == ["Токио, 12-17 ноября"] and history[0]["role"] == "user"
    async with db.sessionmaker() as s:
        assert len((await s.scalars(select(AgentRun))).all()) == 1


async def test_empty_message_is_a_validation_error(client):
    h = await login(client)
    chat = (await client.get("/api/v1/chats/active", headers=h)).json()
    assert (await client.post(f"/api/v1/chats/{chat['id']}/messages", headers=h, json={"text": ""})).status_code == 422


async def test_a_user_cannot_see_someone_elses_chat(client):
    alice, bob = await login(client, 1), await login(client, 2)
    chat = (await client.get("/api/v1/chats/active", headers=alice)).json()
    for path in (f"/messages", f"/plan"):
        assert (await client.get(f"/api/v1/chats/{chat['id']}{path}", headers=bob)).status_code == 404
    assert (await client.post(f"/api/v1/chats/{chat['id']}/messages", headers=bob, json={"text": "hi"})).status_code == 404
    assert (await client.get(f"/api/v1/chats/{chat['id']}/events", headers=bob)).status_code == 404


async def test_creating_a_chat_archives_the_old_one(client, db):
    h = await login(client)
    old = (await client.get("/api/v1/chats/active", headers=h)).json()
    new = (await client.post("/api/v1/chats", headers=h)).json()
    assert new["id"] != old["id"]
    assert (await client.get("/api/v1/chats/active", headers=h)).json()["id"] == new["id"]
    async with db.sessionmaker() as s:
        assert (await s.get(Chat, uuid.UUID(old["id"]))).status == "archived"


async def test_plan_and_versions(client, db):
    h = await login(client)
    chat = (await client.get("/api/v1/chats/active", headers=h)).json()
    assert (await client.get(f"/api/v1/chats/{chat['id']}/plan", headers=h)).status_code == 404
    async with db.sessionmaker() as s:
        plan = TripPlan(chat_id=uuid.UUID(chat["id"]), revision=2, status="ready", view_json={"title": "v2"})
        s.add(plan)
        await s.flush()
        s.add(TripPlanVersion(plan_id=plan.id, revision=1, view_json={"title": "v1"}))
        s.add(TripPlanVersion(plan_id=plan.id, revision=2, view_json={"title": "v2"}))
        await s.commit()
        plan_id = plan.id
    current = (await client.get(f"/api/v1/chats/{chat['id']}/plan", headers=h)).json()
    assert current["view"] == {"title": "v2"} and current["revision"] == 2
    old = (await client.get(f"/api/v1/plans/{plan_id}/versions/1", headers=h)).json()
    assert old["view"] == {"title": "v1"} and old["status"] == "snapshot"
    assert (await client.get(f"/api/v1/plans/{plan_id}/versions/9", headers=h)).status_code == 404
    other = await login(client, 99)
    assert (await client.get(f"/api/v1/plans/{plan_id}/versions/1", headers=other)).status_code == 404


async def test_answering_an_unknown_question_is_a_conflict(client):
    h = await login(client)
    chat = (await client.get("/api/v1/chats/active", headers=h)).json()
    r = await client.post(f"/api/v1/chats/{chat['id']}/clarifications/{uuid.uuid4()}/answers", headers=h,
                          json={"answer": "да"})
    assert r.status_code == 409


# ---- SSE ----

async def collect(db, chat_id, after, **kw):
    return [chunk async for chunk in event_stream(db, chat_id, after, poll_interval=0.01, **kw)]


async def test_event_stream_replays_from_a_cursor_in_order(db):
    async with db.sessionmaker() as s:
        user = User(telegram_id=1)
        s.add(user)
        await s.flush()
        chat = Chat(user_id=user.id)
        s.add(chat)
        await s.flush()
        for i in range(3):
            await emit_event(s, chat.id, EventType.AGENT_PROGRESS, AgentProgress(text=f"step {i}"))
        await s.commit()
        chat_id = chat.id
    everything = await collect(db, chat_id, 0, max_seconds=0.05)
    assert len(everything) == 3 and "event: agent.progress" in everything[0]
    first_seq = int(everything[0].split("\n")[0].removeprefix("id: "))
    resumed = await collect(db, chat_id, first_seq, max_seconds=0.05)
    assert len(resumed) == 2 and "step 1" in resumed[0]


async def test_event_stream_emits_keepalive_when_idle(db):
    chunks = await collect(db, uuid.uuid4(), 0, max_seconds=0.1, heartbeat=0.02)
    assert chunks and all(c.startswith(": keep-alive") for c in chunks)
