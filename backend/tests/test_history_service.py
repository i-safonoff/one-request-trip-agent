"""History service on a real (in-memory) database."""
import json
import uuid
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from history.models import PreferenceItem, TripFeedback, TripTemplate
from history.service import HistoryService, Invalid, NotEligible, NotFound
from shared.config import get_settings
from shared.events import EventType
from shared.models import Chat, ChatEvent, InheritedItem, TripPlan, TripPlanVersion, User
from shared.templates import apply_items

EXAMPLES = Path(__file__).resolve().parent.parent / "trip_map" / "examples"
JAPAN = json.loads((EXAMPLES / "trip.json").read_text("utf-8"))
AFTER = date(2027, 1, 1)  # the Japan mock ends 2026-11-21
DURING = date(2026, 11, 15)


async def seed_user(db, telegram_id=42) -> uuid.UUID:
    async with db.sessionmaker() as s:
        user = await s.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            user = User(telegram_id=telegram_id, first_name="Ann")
            s.add(user)
            await s.commit()
        return user.id


async def seed_trip(db, user_id, state=None, title=None, status="active") -> uuid.UUID:
    state = {**(state or JAPAN), **({"title": title} if title else {})}
    async with db.sessionmaker() as s:
        chat = Chat(user_id=user_id, telegram_chat_id=42, status=status)
        s.add(chat)
        await s.flush()
        s.add(TripPlan(chat_id=chat.id, revision=1, status="ready", state_json=state, view_json=state))
        await s.commit()
        return chat.id


async def svc(db):
    session = db.sessionmaker()
    return HistoryService(session), session


async def rate(db, user, chat, node="stay_osaka", sentiment=1, aspects=None, today=AFTER):
    s, session = await svc(db)
    async with session:
        return await s.submit_feedback(user, chat, node, sentiment, aspects, None, today)


# ---- history and eligibility ----

async def test_history_lists_trips_with_chosen_stays_newest_first(db):
    user = await seed_user(db)
    await seed_trip(db, user, title="Прошлый", status="archived")
    await seed_trip(db, user, title="Новый")
    empty = await seed_trip(db, user, state={"sequence": [], "nodes": {}, "offers": {}})  # no stays: not history
    s, session = await svc(db)
    async with session:
        entries = await s.trips(user, today=AFTER)
    assert [e.title for e in entries] == ["Новый", "Прошлый"] and empty not in [e.chat_id for e in entries]
    e = entries[0]
    assert e.completed and e.can_give_feedback and (e.date_from, e.date_to) == (date(2026, 11, 12), date(2026, 11, 21))
    assert [(x.node_id, x.name, x.nights) for x in e.stays] == [("stay_tokyo", "Shinjuku Granbell Hotel", 4), ("stay_osaka", "Cross Hotel Osaka", 4)]


async def test_a_trip_that_has_not_happened_cannot_be_rated(db):
    user, chat = await seed_user(db), None
    chat = await seed_trip(db, user)
    s, session = await svc(db)
    async with session:
        assert not (await s.trips(user, today=DURING))[0].can_give_feedback
    with pytest.raises(NotEligible):
        await rate(db, user, chat, today=DURING)


async def test_the_dev_switch_allows_early_feedback(db, monkeypatch):
    monkeypatch.setenv("HISTORY_ALLOW_EARLY_FEEDBACK", "true")
    get_settings.cache_clear()
    user = await seed_user(db)
    chat = await seed_trip(db, user)
    assert (await rate(db, user, chat, today=DURING)).sentiment == 1


# ---- feedback validation ----

async def test_feedback_is_validated_and_private(db):
    alice, bob = await seed_user(db, 1), await seed_user(db, 2)
    chat = await seed_trip(db, alice)
    for kwargs in ({"sentiment": 2}, {"aspects": {"vibes": 1}}, {"aspects": {"price": 0}}, {"node": "stay_nowhere"}):
        with pytest.raises(Invalid):
            await rate(db, alice, chat, **{"sentiment": 1, **kwargs})
    with pytest.raises(NotFound):
        await rate(db, bob, chat)  # someone else's trip looks like a missing one


async def test_feedback_is_one_row_per_stay_and_is_edited_in_place(db):
    user = await seed_user(db)
    chat = await seed_trip(db, user)
    await rate(db, user, chat, sentiment=1)
    await rate(db, user, chat, sentiment=-1, aspects={"price": -1})
    async with db.sessionmaker() as s:
        (row,) = (await s.scalars(select(TripFeedback))).all()
    assert (row.sentiment, row.aspects) == (-1, {"price": -1})


# ---- the learned profile ----

async def profile(db, user):
    s, session = await svc(db)
    async with session:
        return {p.item_key: p for p in await s.profile(user)}


async def test_feedback_builds_the_profile_with_evidence(db):
    user = await seed_user(db)
    chat = await seed_trip(db, user, title="Токио и Осака")
    await rate(db, user, chat, sentiment=1, aspects={"meal": 1, "location": 1})
    p = await profile(db, user)
    assert {"constraint:breakfast", "priority:location", "hotel:like:cross hotel osaka"} <= set(p)
    b = p["constraint:breakfast"]
    assert b.confidence == pytest.approx(0.6, abs=0.01) and b.observed_count == 1
    assert b.evidence[0]["trip_title"] == "Токио и Осака" and b.evidence[0]["hotel_name"] == "Cross Hotel Osaka"


async def test_changing_or_withdrawing_feedback_changes_the_profile_it_is_derived_from(db):
    user = await seed_user(db)
    chat = await seed_trip(db, user)
    await rate(db, user, chat, sentiment=1, aspects={"meal": 1})
    assert "constraint:breakfast" in await profile(db, user)
    await rate(db, user, chat, sentiment=0, aspects={})  # changed their mind: no evidence left
    assert await profile(db, user) == {}


async def test_trip_level_feedback_counts_for_unrated_stays_only_and_weaker(db):
    user = await seed_user(db)
    chat = await seed_trip(db, user)
    await rate(db, user, chat, node="", sentiment=1, aspects={"meal": 1})
    p = await profile(db, user)
    assert p["constraint:breakfast"].confidence == pytest.approx(0.3, abs=0.01)  # 0.6 x 0.5, only Osaka had breakfast
    assert "hotel:like:cross hotel osaka" in p  # the trip was liked: its hotels are known to be fine


# ---- suggestions ----

async def new_chat(db, user) -> uuid.UUID:
    async with db.sessionmaker() as s:
        chat = Chat(user_id=user, telegram_chat_id=42)
        s.add(chat)
        await s.commit()
        return chat.id


async def propose(db, chat, **kw):
    s, session = await svc(db)
    async with session:
        return await s.propose(chat, **kw)


async def test_one_weak_signal_is_not_proposed_but_a_repeated_pattern_is(db):
    user = await seed_user(db)
    first = await seed_trip(db, user, title="Первая", status="archived")
    await rate(db, user, first, sentiment=1)  # a bare thumbs-up on one stay: weak hints only (0.35)
    assert await propose(db, await new_chat(db, user)) == []
    second = await seed_trip(db, user, title="Вторая", status="archived")
    await rate(db, user, second, sentiment=1)
    keys = {i.item_key for i in await propose(db, await new_chat(db, user))}
    assert "constraint:breakfast" in keys  # two independent trips: 1 - 0.65 * 0.65 = 0.58, over the 0.4 threshold


async def test_a_bare_thumbs_up_on_a_whole_trip_is_too_vague_to_propose_anything(db):
    user = await seed_user(db)
    for title in ("Первая", "Вторая"):
        await rate(db, user, await seed_trip(db, user, title=title, status="archived"), node="", sentiment=1)
    keys = {i.item_key for i in await propose(db, await new_chat(db, user), limit=9)}
    assert "constraint:breakfast" not in keys  # 0.175 per trip: 0.32 after two, still under 0.4


async def test_proposals_carry_a_reason_and_are_announced_by_an_event(db):
    user = await seed_user(db)
    old = await seed_trip(db, user, title="Токио и Осака", status="archived")
    await rate(db, user, old, sentiment=1, aspects={"meal": 1, "location": 1})
    chat = await new_chat(db, user)
    items = await propose(db, chat)
    by_key = {i.item_key: i for i in items}
    assert by_key["constraint:breakfast"].reason == "В поездке «Токио и Осака» вам это понравилось"
    assert by_key["constraint:breakfast"].status == "proposed"
    async with db.sessionmaker() as s:
        event = await s.scalar(select(ChatEvent).where(ChatEvent.chat_id == chat))
    assert event.type == EventType.SUGGESTIONS_PROPOSED.value
    assert {i["text"] for i in event.payload["items"]} >= {"Завтрак включён", "Хорошее расположение"}


async def test_each_key_is_proposed_once_per_trip_and_at_most_three_at_a_time(db):
    user = await seed_user(db)
    old = await seed_trip(db, user, status="archived")
    await rate(db, user, old, sentiment=1, aspects={"meal": 1, "location": 1, "stars": 1, "cancellation": 1})
    chat = await new_chat(db, user)
    assert len(await propose(db, chat, limit=3)) == 3
    again = await propose(db, chat, limit=3)  # the rest, never the same three
    assert all(i.status == "proposed" for i in again)
    async with db.sessionmaker() as s:
        keys = [r.item_key for r in await s.scalars(select(InheritedItem).where(InheritedItem.chat_id == chat))]
    assert len(keys) == len(set(keys))


async def test_a_hotel_hint_is_offered_only_for_the_same_destination(db):
    user = await seed_user(db)
    old = await seed_trip(db, user, status="archived")
    await rate(db, user, old, sentiment=1)
    assert not [i for i in await propose(db, await new_chat(db, user), limit=9) if i.item_key.startswith("hotel:")]
    assert not [i for i in await propose(db, await new_chat(db, user), place_name="Казань", limit=9) if i.item_key.startswith("hotel:")]
    hints = [i for i in await propose(db, await new_chat(db, user), place_name="осака", limit=9) if i.item_key.startswith("hotel:")]
    assert [h.item["name"] for h in hints] == ["Cross Hotel Osaka"]


async def decide(db, user, sid, accept):
    s, session = await svc(db)
    async with session:
        return await s.decide(user, sid, accept)


async def test_accepting_makes_it_available_to_the_agents_and_it_cannot_be_decided_twice(db):
    user = await seed_user(db)
    old = await seed_trip(db, user, title="Токио", status="archived")
    await rate(db, user, old, sentiment=1, aspects={"meal": 1})
    chat = await new_chat(db, user)
    item = next(i for i in await propose(db, chat) if i.item_key == "constraint:breakfast")
    assert (await decide(db, user, item.id, True)).status == "accepted"
    assert await decide(db, user, item.id, False) is None  # already decided
    s, session = await svc(db)
    async with session:
        accepted = await s.accepted(chat)
    assert len(accepted) == 1
    config = apply_items({"constraints": []}, accepted)
    assert config["constraints"][0]["kind"] == "breakfast" and config["constraints"][0]["strength"] == "soft"
    assert config["constraints"][0]["source"]["origin"] == "history"


async def test_only_the_owner_can_decide(db):
    alice, bob = await seed_user(db, 1), await seed_user(db, 2)
    old = await seed_trip(db, alice, status="archived")
    await rate(db, alice, old, sentiment=1, aspects={"meal": 1})
    item = (await propose(db, await new_chat(db, alice)))[0]
    assert await decide(db, bob, item.id, True) is None
    assert (await decide(db, alice, item.id, True)).status == "accepted"


async def test_dismissing_the_same_thing_in_two_trips_stops_it_for_good(db):
    user = await seed_user(db)
    old = await seed_trip(db, user, status="archived")
    await rate(db, user, old, sentiment=1, aspects={"meal": 1})
    for _ in range(2):
        item = next(i for i in await propose(db, await new_chat(db, user)) if i.item_key == "constraint:breakfast")
        await decide(db, user, item.id, False)
    assert (await profile(db, user))["constraint:breakfast"].muted
    assert "constraint:breakfast" not in {i.item_key for i in await propose(db, await new_chat(db, user))}


async def test_forget_deletes_everything_the_module_knows(db):
    user = await seed_user(db)
    old = await seed_trip(db, user, status="archived")
    await rate(db, user, old, sentiment=1, aspects={"meal": 1})
    await propose(db, await new_chat(db, user))
    s, session = await svc(db)
    async with session:
        await s.save_template(user, old, "Япония")
        counts = await s.forget_all(user)
    assert counts["feedback"] == 1 and counts["profile"] >= 1 and counts["suggestions"] >= 1 and counts["templates"] == 1
    async with db.sessionmaker() as s2:
        for model in (TripFeedback, PreferenceItem, InheritedItem, TripTemplate):
            assert (await s2.scalars(select(model))).all() == []
        assert (await s2.scalars(select(TripPlan))).all()  # the trips themselves are not touched


# ---- templates ----

async def test_save_and_start_from_a_template_creates_a_draft_trip(db):
    user = await seed_user(db)
    old = await seed_trip(db, user, title="Токио и Осака")
    s, session = await svc(db)
    async with session:
        tpl = await s.save_template(user, old, "Япония весной")
        chat, plan = await s.start_from_template(user, tpl.id, date(2027, 3, 10))
    assert plan.status == "draft" and plan.state_json["title"] == "Как в «Япония весной»" and plan.state_json["offers"] == {}
    assert [plan.state_json["nodes"][n]["type"] for n in plan.state_json["sequence"]].count("stay") == 2
    async with db.sessionmaker() as s2:
        assert (await s2.get(Chat, old)).status == "archived" and (await s2.get(Chat, chat.id)).status == "active"
        assert (await s2.get(Chat, chat.id)).telegram_chat_id == 42
        assert len((await s2.scalars(select(TripPlanVersion))).all()) == 1


async def test_extends_has_depth_one_and_resolution_merges(db):
    user = await seed_user(db)
    chat = await seed_trip(db, user)
    s, session = await svc(db)
    async with session:
        parent = await s.save_template(user, chat, "База")
        child = await s.save_template(user, chat, "Вариант", parent_id=parent.id)
        with pytest.raises(Invalid):
            await s.save_template(user, chat, "Внук", parent_id=child.id)  # parent already extends something
        row, resolved = await s.resolved(user, child.id)
    assert row.parent_id == parent.id and resolved.provenance["route"] == "child"


async def test_templates_are_private_to_their_owner(db):
    alice, bob = await seed_user(db, 1), await seed_user(db, 2)
    chat = await seed_trip(db, alice)
    s, session = await svc(db)
    async with session:
        tpl = await s.save_template(alice, chat, "Моя")
        for call in (s.resolved(bob, tpl.id), s.start_from_template(bob, tpl.id), s.save_template(bob, chat, "Чужая")):
            with pytest.raises(NotFound):
                await call
        with pytest.raises(NotFound):
            await s.save_template(alice, chat, "Дочь", parent_id=uuid.uuid4())
