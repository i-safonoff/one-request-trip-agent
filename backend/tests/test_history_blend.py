"""Blending past trips into a new one: ingredients of a trip and the blend that stores the picks."""
import pytest
from sqlalchemy import select

from history.essence import CONTEXT, HOTEL, TASTE, essence
from history.service import Invalid, NotFound
from shared.models import InheritedItem
from shared.templates import apply_items
from tests.test_chat_api import login
from tests.test_history_api import client  # noqa: F401
from tests.test_history_service import AFTER, JAPAN, rate, seed_trip, seed_user, svc

JP = {**JAPAN, "title": "Япония",
      "constraints": [{"kind": "breakfast", "value": True}, {"kind": "free_cancellation", "value": True},
                      {"kind": "not_in_vocabulary", "value": 1}],
      "priorities": [{"key": "location", "weight": 2.0}],
      "party": {"adults": 2, "children_ages": [5]},
      "budget": {"amount": "300000", "currency": "RUB", "scope": "trip"}}
TR = {**JAPAN, "title": "Турция", "constraints": [{"kind": "free_cancellation", "value": True},
                                                  {"kind": "min_stars", "value": 4}],
      "priorities": [{"key": "price", "weight": 1.5}], "party": {"adults": 2}}


def test_tastes_are_on_and_context_is_off_but_present():
    ings = {i.key: i for i in essence(JP, liked=[])}
    assert {k for k, i in ings.items() if i.group == TASTE} == {"constraint:breakfast", "constraint:free_cancellation", "priority:location"}
    assert all(i.default_on for i in ings.values() if i.group == TASTE)
    ctx = {k: i for k, i in ings.items() if i.group == CONTEXT}
    assert set(ctx) == {"context:party", "context:budget", "context:nights"}
    assert not any(i.default_on for i in ctx.values())  # context is offered, never applied silently
    assert ctx["context:party"].text == "2 взрослых, ребёнок 5 лет" and ctx["context:party"].phrase == "нас 2 взрослых, ребёнок 5 лет"
    assert ctx["context:budget"].phrase == "бюджет до 300 000 ₽ на поездку"
    assert ctx["context:nights"].phrase == "примерно на 8 ночей"


def test_a_liked_hotel_is_a_hint_tied_to_its_city():
    liked = [{"type": "hotel", "polarity": "like", "name": "Cross Hotel Osaka", "place_name": "Осака"}]
    hotel = [i for i in essence(JP, liked) if i.group == HOTEL][0]
    assert hotel.default_on and "Осака" in hotel.why


def test_a_liked_taste_the_user_also_chose_is_one_ingredient_with_a_stronger_reason():
    liked = [{"type": "constraint", "kind": "breakfast", "value": True, "strength": "soft"}]
    ings = [i for i in essence(JP, liked) if i.key == "constraint:breakfast"]
    assert len(ings) == 1 and "довольны" in ings[0].why


async def test_essence_of_someone_elses_trip_is_not_found(db):
    ann, bob = await seed_user(db, 1), await seed_user(db, 2)
    trip = await seed_trip(db, bob, JP)
    s, session = await svc(db)
    async with session:
        with pytest.raises(NotFound):
            await s.essence(ann, trip)


async def test_essence_includes_what_the_user_liked_after_the_trip(db):
    user = await seed_user(db)
    trip = await seed_trip(db, user, JP)
    await rate(db, user, trip, node="stay_osaka", sentiment=1, today=AFTER)
    s, session = await svc(db)
    async with session:
        await s.rebuild_profile(user)
        title, ings = await s.essence(user, trip)
    assert title == "Япония" and any(i.group == HOTEL and "Cross Hotel Osaka" in i.text for i in ings)


async def test_blend_of_two_trips_stores_tastes_with_both_sources_and_the_agents_can_apply_them(db):
    user = await seed_user(db)
    jp, tr, new = await seed_trip(db, user, JP), await seed_trip(db, user, TR), await seed_trip(db, user, {"sequence": [], "nodes": {}})
    s, session = await svc(db)
    async with session:
        rows = await s.blend(user, new, [("constraint:free_cancellation", jp), ("constraint:breakfast", jp),
                                         ("constraint:min_stars", tr)])
        accepted = await s.accepted(new)
    assert {r.item_key for r in rows} == {"constraint:free_cancellation", "constraint:breakfast", "constraint:min_stars"}
    both = next(r for r in rows if r.item_key == "constraint:free_cancellation")
    assert "«Япония»" in both.reason and "«Турция»" in both.reason  # the same taste from two trips names both
    cfg = apply_items({"constraints": [{"kind": "breakfast", "value": False}]}, accepted)
    assert [c["kind"] for c in cfg["constraints"]] == ["breakfast", "free_cancellation", "min_stars"]
    assert cfg["constraints"][0]["value"] is False  # what the user said in this trip still wins


async def test_a_new_blend_replaces_the_old_one_but_leaves_other_suggestions_alone(db):
    user = await seed_user(db)
    jp, new = await seed_trip(db, user, JP), await seed_trip(db, user, {"sequence": [], "nodes": {}})
    async with (await svc(db))[1] as session:
        session.add(InheritedItem(chat_id=new, user_id=user, item_key="priority:rating", reason="бот предложил",
                                  item={"type": "priority", "key": "rating", "weight": 1.5}, status="accepted"))
        await session.commit()
    s, session = await svc(db)
    async with session:
        await s.blend(user, new, [("constraint:breakfast", jp), ("priority:location", jp)])
        await s.blend(user, new, [("priority:location", jp)])  # the user pulled breakfast back out
        rows = {r.item_key: r.status for r in await session.scalars(select(InheritedItem).where(InheritedItem.chat_id == new))}
    assert rows == {"constraint:breakfast": "dismissed", "priority:location": "accepted", "priority:rating": "accepted"}


async def test_blend_rejects_context_and_foreign_keys(db):
    user = await seed_user(db)
    jp, new = await seed_trip(db, user, JP), await seed_trip(db, user, {"sequence": [], "nodes": {}})
    s, session = await svc(db)
    async with session:
        for key in ("context:budget", "constraint:pets_allowed"):
            with pytest.raises(Invalid):
                await s.blend(user, new, [(key, jp)])


async def test_blend_api_round_trip(client, db):  # noqa: F811 - the fixture imported above
    h = await login(client, 777)
    user = await seed_user(db, 777)
    jp = await seed_trip(db, user, JP)
    new = await seed_trip(db, user, {"sequence": [], "nodes": {}})
    r = await client.get(f"/api/v1/history/trips/{jp}/essence", headers=h)
    assert r.status_code == 200 and r.json()["title"] == "Япония"
    r = await client.post(f"/api/v1/history/chats/{new}/blend", headers=h,
                          json={"picks": [{"key": "constraint:breakfast", "source": str(jp)}]})
    assert r.status_code == 200 and r.json()[0]["status"] == "accepted"
