import uuid

import httpx
import pytest

import trip_map
from main import app
from shared.config import get_settings

from .test_chat_api import login
from .test_history_service import AFTER, new_chat, seed_trip, seed_user  # noqa: F401


@pytest.fixture
async def client(db, monkeypatch):
    monkeypatch.setenv("HISTORY_ALLOW_EARLY_FEEDBACK", "true")  # the mock trips are in the future
    get_settings.cache_clear()
    app.dependency_overrides[trip_map.get_geocoder] = lambda: (lambda name: None)  # no network in tests
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.pop(trip_map.get_geocoder, None)




async def setup(db, client, tg=42, title="Токио и Осака", status="archived"):
    h = await login(client, tg)
    uid = await seed_user(db, tg)
    chat = await seed_trip(db, uid, title=title, status=status)
    return h, uid, chat


async def test_history_feedback_profile_and_suggestions_end_to_end(client, db):
    h, uid, old = await setup(db, client)
    trips = (await client.get("/api/v1/history/trips", headers=h)).json()
    assert [t["title"] for t in trips] == ["Токио и Осака"] and trips[0]["stays"][1]["name"] == "Cross Hotel Osaka"

    r = await client.post(f"/api/v1/history/trips/{old}/feedback", headers=h,
                          json={"node_id": "stay_osaka", "sentiment": 1, "aspects": {"meal": 1, "location": 1}})
    assert r.status_code == 200 and r.json()["aspects"] == {"meal": 1, "location": 1}

    prof = (await client.get("/api/v1/history/profile", headers=h)).json()
    breakfast = next(p for p in prof if p["key"] == "constraint:breakfast")
    assert breakfast["text"] == "Завтрак включён" and breakfast["why"] == ["Токио и Осака"] and breakfast["observed_trips"] == 1

    new = await new_chat(db, uid)
    sug = (await client.post(f"/api/v1/history/chats/{new}/suggestions", headers=h)).json()
    item = next(s for s in sug if s["key"] == "constraint:breakfast")
    assert item["status"] == "proposed" and item["reason"] == "В поездке «Токио и Осака» вам это понравилось"
    done = await client.post(f"/api/v1/history/suggestions/{item['id']}/accept", headers=h)
    assert done.status_code == 200 and done.json()["status"] == "accepted"
    assert (await client.post(f"/api/v1/history/suggestions/{item['id']}/dismiss", headers=h)).status_code == 409
    assert (await client.post(f"/api/v1/history/suggestions/{item['id']}/maybe", headers=h)).status_code == 404
    listed = (await client.get(f"/api/v1/history/chats/{new}/suggestions", headers=h)).json()
    assert {s["status"] for s in listed} >= {"accepted", "proposed"}


async def test_mute_and_forget(client, db):
    h, uid, old = await setup(db, client)
    await client.post(f"/api/v1/history/trips/{old}/feedback", headers=h,
                      json={"node_id": "stay_osaka", "sentiment": 1, "aspects": {"meal": 1}})
    item = (await client.get("/api/v1/history/profile", headers=h)).json()[0]
    assert (await client.post(f"/api/v1/history/profile/items/{item['id']}/mute", headers=h)).status_code == 204
    assert next(p for p in (await client.get("/api/v1/history/profile", headers=h)).json() if p["id"] == item["id"])["muted"]
    erased = (await client.delete("/api/v1/history/profile", headers=h)).json()
    assert erased["feedback"] == 1 and (await client.get("/api/v1/history/profile", headers=h)).json() == []


async def test_feedback_errors_are_explicit(client, db):
    h, uid, old = await setup(db, client)
    bad = await client.post(f"/api/v1/history/trips/{old}/feedback", headers=h, json={"sentiment": 5})
    assert bad.status_code == 422
    assert (await client.post(f"/api/v1/history/trips/{uuid.uuid4()}/feedback", headers=h, json={"sentiment": 1})).status_code == 404
    assert (await client.get("/api/v1/history/trips")).status_code == 401


async def test_a_future_trip_cannot_be_rated_without_the_dev_switch(db, monkeypatch):
    monkeypatch.delenv("HISTORY_ALLOW_EARLY_FEEDBACK", raising=False)
    get_settings.cache_clear()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        h, uid, old = await setup(db, client)
        r = await client.post(f"/api/v1/history/trips/{old}/feedback", headers=h, json={"node_id": "stay_osaka", "sentiment": 1})
    assert r.status_code == 409 and "после поездки" in r.json()["detail"]


async def test_a_user_sees_only_their_own_trips_profile_and_templates(client, db):
    alice, uid_a, chat_a = await setup(db, client, tg=1)
    bob, uid_b, _ = await setup(db, client, tg=2, title="Чужая")
    await client.post(f"/api/v1/history/trips/{chat_a}/feedback", headers=alice, json={"node_id": "stay_osaka", "sentiment": 1, "aspects": {"meal": 1}})
    assert [t["title"] for t in (await client.get("/api/v1/history/trips", headers=bob)).json()] == ["Чужая"]
    assert (await client.get("/api/v1/history/profile", headers=bob)).json() == []
    assert (await client.post(f"/api/v1/history/trips/{chat_a}/feedback", headers=bob, json={"sentiment": 1})).status_code == 404
    tpl = (await client.post("/api/v1/history/templates", headers=alice, json={"chat_id": str(chat_a), "name": "Моя"})).json()
    assert (await client.get(f"/api/v1/history/templates/{tpl['id']}", headers=bob)).status_code == 404
    assert (await client.post(f"/api/v1/history/templates/{tpl['id']}/start", headers=bob, json={})).status_code == 404


async def test_templates_over_http(client, db):
    h, uid, old = await setup(db, client)
    tpl = (await client.post("/api/v1/history/templates", headers=h, json={"chat_id": str(old), "name": "Япония"})).json()
    assert tpl["stops"] == ["Токио", "Осака"] and tpl["parent_id"] is None
    child = await client.post("/api/v1/history/templates", headers=h, json={"chat_id": str(old), "name": "Вариант", "parent_id": tpl["id"]})
    assert child.status_code == 201
    grandchild = await client.post("/api/v1/history/templates", headers=h, json={"chat_id": str(old), "name": "Внук", "parent_id": child.json()["id"]})
    assert grandchild.status_code == 422  # depth 1
    assert len((await client.get("/api/v1/history/templates", headers=h)).json()) == 2
    resolved = (await client.get(f"/api/v1/history/templates/{child.json()['id']}", headers=h)).json()
    assert resolved["provenance"]["route"] == "child"
    started = (await client.post(f"/api/v1/history/templates/{tpl['id']}/start", headers=h, json={"start_date": "2027-03-10"})).json()
    assert started["title"] == "Как в «Япония»"
    plan = (await client.get(f"/trip-map/api/trip", params={"plan_id": started["plan_id"]}, headers=h))
    assert plan.status_code == 200 and all(s["offer"] is None for s in plan.json()["steps"])  # the map shows ghost pins
