"""The Mini App path: the map shows the signed-in user's plan from the database."""
import json
import uuid

import httpx
import pytest
from aiogram.methods import SetChatMenuButton, SetWebhook

import trip_map
from chat.dev_agent import EXAMPLES, pick_mock
from chat.lifecycle import chat_lifespan
from main import app
from shared.models import TripPlan

from .fake_telegram import make_bot
from .test_chat_api import login, make_init_data  # noqa: F401  (make_init_data used by login)

FAKE_COORDS = {}


@pytest.fixture
async def client(db):
    app.dependency_overrides[trip_map.get_geocoder] = lambda: (lambda name: FAKE_COORDS.get(name))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.pop(trip_map.get_geocoder, None)


async def seed_plan(db, client, headers, view=None):
    chat = (await client.get("/api/v1/chats/active", headers=headers)).json()
    async with db.sessionmaker() as s:
        plan = TripPlan(chat_id=uuid.UUID(chat["id"]), revision=1, status="ready",
                        view_json=view or json.loads((EXAMPLES / "trip_moscow_spb.json").read_text("utf-8")))
        s.add(plan)
        await s.commit()
        return plan.id


async def test_latest_plan_of_the_active_trip_is_shown(client, db):
    h = await login(client)
    await seed_plan(db, client, h)
    r = await client.get("/trip-map/api/trip", params={"plan_id": "latest"}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["title"].startswith("Москва и Санкт-Петербург")
    assert [s["id"] for s in body["steps"]] == ["stay_moscow", "moscow_spb", "stay_spb"]
    assert body["steps"][0]["offer"]["name"] == "Radisson Collection Hotel, Moscow"


async def test_a_plan_can_be_opened_by_its_id(client, db):
    h = await login(client)
    plan_id = await seed_plan(db, client, h)
    r = await client.get("/trip-map/api/trip", params={"plan_id": str(plan_id)}, headers=h)
    assert r.status_code == 200 and r.json()["trip_id"] == "demo_moscow_spb"


async def test_latest_without_a_plan_says_so(client):
    h = await login(client)
    r = await client.get("/trip-map/api/trip", params={"plan_id": "latest"}, headers=h)
    assert r.status_code == 404 and "Опишите поездку боту" in r.json()["detail"]


async def test_a_plan_needs_a_signed_in_user(client, db):
    h = await login(client)
    plan_id = await seed_plan(db, client, h)
    assert (await client.get("/trip-map/api/trip", params={"plan_id": str(plan_id)})).status_code == 401
    bad = {"Authorization": "Bearer nope.nope"}
    assert (await client.get("/trip-map/api/trip", params={"plan_id": "latest"}, headers=bad)).status_code == 401


async def test_someone_elses_plan_looks_like_a_missing_one(client, db):
    alice, bob = await login(client, 1), await login(client, 2)
    plan_id = await seed_plan(db, client, alice)
    r = await client.get("/trip-map/api/trip", params={"plan_id": str(plan_id)}, headers=bob)
    assert r.status_code == 404
    assert (await client.get("/trip-map/api/trip", params={"plan_id": str(plan_id)}, headers=alice)).status_code == 200


async def test_an_archived_trip_stays_readable_by_id_but_is_not_latest(client, db):
    h = await login(client)
    plan_id = await seed_plan(db, client, h)
    await client.post("/api/v1/chats", headers=h)  # /new: archives the chat that owns the plan
    assert (await client.get("/trip-map/api/trip", params={"plan_id": str(plan_id)}, headers=h)).status_code == 200
    assert (await client.get("/trip-map/api/trip", params={"plan_id": "latest"}, headers=h)).status_code == 404


async def test_bad_plan_id_and_unrenderable_plan(client, db):
    h = await login(client)
    assert (await client.get("/trip-map/api/trip", params={"plan_id": "abc"}, headers=h)).status_code == 422
    await seed_plan(db, client, h, view={"title": "no graph here"})
    r = await client.get("/trip-map/api/trip", params={"plan_id": "latest"}, headers=h)
    assert r.status_code == 422 and "нельзя показать" in r.json()["detail"]


async def test_without_plan_id_the_bundled_example_still_works(client):
    r = await client.get("/trip-map/api/trip")
    assert r.status_code == 200 and r.json()["steps"]


def test_mock_depends_on_the_whole_conversation():
    assert pick_mock(["Хочу в Москву", "Двое на неделю"])["trip_id"] == "demo_moscow_spb"
    assert pick_mock(["Поездка в Питер на выходные"])["trip_id"] == "demo_moscow_spb"
    assert pick_mock(["Хочу в Японию", "Двое"])["places"]["tokyo"]["name"] == "Токио"


@pytest.mark.parametrize("base_url, expect_button", [("https://example.test", True), ("http://localhost:8080", False)])
async def test_the_bot_gets_a_map_menu_button_only_with_https(db, monkeypatch, base_url, expect_button):
    from chat import lifecycle
    from shared.config import get_settings
    monkeypatch.setenv("PUBLIC_BASE_URL", base_url)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "https://example.test/telegram/webhook")
    get_settings.cache_clear()
    bot, session = make_bot()
    monkeypatch.setattr(lifecycle, "create_bot", lambda token: bot)
    async with chat_lifespan(app):
        pass
    buttons = session.of(SetChatMenuButton)
    assert buttons  # always set: with http a stale Mini App button of an earlier run is reset to the default
    if expect_button:
        assert buttons[0].menu_button.web_app.url == "https://example.test/"
    else:
        assert buttons[0].menu_button.type == "default"
    assert session.of(SetWebhook)  # the webhook is registered either way
