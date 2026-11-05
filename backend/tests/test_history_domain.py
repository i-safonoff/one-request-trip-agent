"""History domain logic: pure functions, no database."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from history import facts
from history.aggregation import aggregate
from history.extraction import Candidate, extract
from history.skeleton import TemplateFragment, instantiate, resolve, skeleton_from_state
from shared.templates import ConstraintItem, HotelHint, PriorityItem, apply_items as _apply, parse_item

EXAMPLES = Path(__file__).resolve().parent.parent / "trip_map" / "examples"
JAPAN = json.loads((EXAMPLES / "trip.json").read_text("utf-8"))
NOW = datetime(2027, 1, 1, tzinfo=timezone.utc)


def stay(**kw) -> facts.StayFacts:
    base = dict(node_id="s1", place_name="Токио", nights=3, hotel_id="h1", name="Hotel One", stars=4, rating=8.7,
                breakfast=True, free_cancellation=None, kid_friendly=None, pets_allowed=None)
    return facts.StayFacts(**{**base, **kw})


def ev(chat="c1", at=NOW, **kw):
    return {"chat_id": chat, "node_id": "s1", "feedback_id": "f", "trip_title": "Тест", "hotel_name": "Hotel One",
            "at": at.isoformat(), **kw}


def keys(cands):
    return {c.item.key: c.confidence for c in cands}


# ---- contract / vocabulary ----

@pytest.mark.parametrize("bad", [
    {"type": "constraint", "kind": "min_stars", "value": 9}, {"type": "constraint", "kind": "wifi"},
    {"type": "constraint", "kind": "breakfast", "value": "yes"}, {"type": "priority", "key": "vibes"},
    {"type": "priority", "key": "price", "weight": 99}, {"type": "constraint", "kind": "only_modes", "value": []}])
def test_items_outside_the_agents_vocabulary_are_rejected(bad):
    with pytest.raises(ValueError):
        parse_item(bad)


def test_apply_items_never_overrides_what_the_user_said_in_this_trip():
    config = {"constraints": [{"id": "c1", "kind": "breakfast", "value": False, "strength": "hard"}],
              "priorities": [{"key": "price", "weight": 3.0}]}
    out = _apply(config, [
        (ConstraintItem(kind="breakfast").model_dump(), "из прошлого", "a"),       # the user already decided: skipped
        (ConstraintItem(kind="free_cancellation").model_dump(), "из прошлого", "b"),
        (PriorityItem(key="price", weight=1.5).model_dump(by_alias=True), "из прошлого", "c"),  # skipped
        (PriorityItem(key="location", weight=2).model_dump(by_alias=True), "из прошлого", "d"),
        (HotelHint(polarity="like", name="X", place_name="Токио").model_dump(), "из прошлого", "e")])
    assert [c["kind"] for c in out["constraints"]] == ["breakfast", "free_cancellation"]
    assert out["constraints"][0]["value"] is False and out["constraints"][0]["strength"] == "hard"
    assert out["constraints"][1]["source"] == {"origin": "history", "quote": "из прошлого"}
    assert [p["key"] for p in out["priorities"]] == ["price", "location"] and out["priorities"][0]["weight"] == 3.0
    assert [h["name"] for h in out["history_hints"]] == ["X"]
    assert config["constraints"][0]["value"] is False and "history_hints" not in config  # input untouched


# ---- reading plans ----

def test_stays_from_the_flat_graph_config():
    s = facts.stays(JAPAN)
    assert [f.node_id for f in s] == ["stay_tokyo", "stay_osaka"]
    assert (s[0].name, s[0].stars, s[0].nights, s[0].place_name) == ("Shinjuku Granbell Hotel", 4, 4, "Токио")
    assert s[0].breakfast is None and s[1].breakfast is True  # unknown is not "no"


def test_stays_from_the_agents_trip_state_shape():
    state = {"sequence": ["st"], "places": {"p": {"name": "Казань"}},
             "nodes": {"st": {"type": "stay", "place": "p", "check_in": "2026-05-01", "check_out": "2026-05-04",
                              "selected_offer_ref": "o1"}},
             "offers": {"o1": {"kind": "hotel_rate", "hotel": {"provider_hotel_id": "77", "name": "Мирас", "stars": 5,
                                                              "rating": 9.1, "kid_friendly": True},
                               "rate": {"breakfast_included": False, "free_cancellation": True}}}}
    (f,) = facts.stays(state)
    assert (f.hotel_id, f.breakfast, f.free_cancellation, f.kid_friendly, f.nights) == ("77", False, True, True, 3)


def test_unchosen_stays_say_nothing_about_taste():
    state = {"sequence": ["a"], "nodes": {"a": {"type": "stay", "place": "p", "selected_offer_ref": None}}, "offers": {}}
    assert facts.stays(state) == []


# ---- extraction ----

def test_liked_meal_with_known_breakfast_becomes_a_breakfast_constraint_and_a_hotel_hint():
    got = keys(extract(stay(), 1, {"meal": 1}, ev()))
    assert got == {"hotel:like:h1": 0.6, "constraint:breakfast": 0.6}


def test_a_breakfast_that_is_unknown_is_never_inferred():
    assert "constraint:breakfast" not in keys(extract(stay(breakfast=None), 1, {"meal": 1}, ev()))


def test_a_bare_thumbs_up_gives_only_weak_hints():
    got = keys(extract(stay(), 1, {}, ev()))
    assert got["constraint:breakfast"] == 0.35 and got["constraint:min_stars"] == 0.3 and got["hotel:like:h1"] == 0.6


def test_dislike_avoids_the_hotel_and_a_disliked_price_means_price_matters_more():
    got = keys(extract(stay(), -1, {"price": -1}, ev()))
    assert got == {"hotel:avoid:h1": 0.7, "priority:price": 0.55}  # not "avoid hotels with breakfast"


def test_disliked_location_class_and_rating_become_needs():
    got = extract(stay(stars=3, rating=7.2), -1, {"location": -1, "stars": -1, "rating": -1}, ev())
    items = {c.item.key: c.item for c in got}
    assert items["priority:location"].weight == 2.0
    assert items["constraint:min_stars"].value == 4 and items["constraint:min_rating"].value == 8.0


def test_trip_level_feedback_is_weighted_down():
    assert keys(extract(stay(), 1, {"meal": 1}, ev(), weight=0.5))["constraint:breakfast"] == 0.3


# ---- aggregation ----

def cand(item, c, chat, at=NOW):
    return Candidate(item=item, confidence=c, evidence=ev(chat=chat, at=at))


def test_independent_trips_add_up_but_stays_of_one_trip_count_once():
    one_trip = [cand(ConstraintItem(kind="breakfast"), 0.4, "a"), cand(ConstraintItem(kind="breakfast"), 0.5, "a")]
    (a,) = aggregate(one_trip, NOW)
    assert a.confidence == 0.5 and a.observed == 1  # the strongest, not 0.7
    two_trips = [cand(ConstraintItem(kind="breakfast"), 0.4, "a"), cand(ConstraintItem(kind="breakfast"), 0.4, "b")]
    (a,) = aggregate(two_trips, NOW)
    assert a.confidence == 0.64 and a.observed == 2  # 1 - 0.6 * 0.6


def test_old_evidence_loses_weight_and_confidence_is_capped():
    (fresh,) = aggregate([cand(ConstraintItem(kind="breakfast"), 0.6, "a")], NOW)
    (old,) = aggregate([cand(ConstraintItem(kind="breakfast"), 0.6, "a", at=NOW - timedelta(days=365))], NOW, 365)
    assert old.confidence == pytest.approx(fresh.confidence / 2, abs=0.01)
    many = [cand(ConstraintItem(kind="breakfast"), 0.9, str(i)) for i in range(8)]
    assert aggregate(many, NOW)[0].confidence == 0.95


def test_numeric_values_are_combined_by_median_not_by_the_extreme():
    stars = [cand(ConstraintItem(kind="min_stars", value=v), 0.5, str(i)) for i, v in enumerate([3, 4, 5])]
    assert aggregate(stars, NOW)[0].item.value == 4
    weights = [cand(PriorityItem(key="price", weight=w), 0.5, str(i)) for i, w in enumerate([2.0, 3.0])]
    assert aggregate(weights, NOW)[0].item.weight == 2.5


def test_the_same_hotel_liked_then_avoided_keeps_the_later_experience():
    like = Candidate(HotelHint(polarity="like", provider_hotel_id="h1", name="A"), 0.6, ev(chat="a", at=NOW - timedelta(days=200)))
    avoid = Candidate(HotelHint(polarity="avoid", provider_hotel_id="h1", name="A"), 0.7, ev(chat="b", at=NOW - timedelta(days=10)))
    assert [a.key for a in aggregate([like, avoid], NOW)] == ["hotel:avoid:h1"]
    assert [a.key for a in aggregate([like, Candidate(avoid.item, 0.7, ev(chat="b", at=NOW - timedelta(days=300)))], NOW)] == ["hotel:like:h1"]


# ---- templates ----

def test_skeleton_keeps_the_route_shape_and_drops_dates_prices_and_offers():
    frag = skeleton_from_state(JAPAN)
    assert [(s.place_name, s.nights) for s in frag.route] == [("Токио", 4), ("Осака", 4)]
    assert [l.mode for l in frag.legs] == ["flight", "drive", "rail", "flight"]
    assert frag.order == ["leg", "leg", "stop", "leg", "stop", "leg"]
    blob = frag.model_dump_json()
    assert not any(w in blob for w in ("2026-", "price", "offer", "Granbell", "74000"))


def test_skeleton_takes_over_valid_criteria_and_ignores_unknown_ones():
    state = {**JAPAN, "constraints": [{"kind": "breakfast", "value": True, "strength": "hard"}, {"kind": "free_text", "value": "тихо"}],
             "priorities": [{"key": "location", "weight": 9}, {"key": "vibes"}]}
    frag = skeleton_from_state(state)
    assert [(c.kind, c.strength) for c in frag.constraints] == [("breakfast", "soft")]  # inherited items are soft
    assert [(p.key_, p.weight) for p in frag.priorities] == [("location", 3.0)]


def test_child_overrides_parent_by_key_and_remove_drops_parent_items():
    parent = TemplateFragment(constraints=[ConstraintItem(kind="breakfast"), ConstraintItem(kind="min_stars", value=4)],
                              priorities=[PriorityItem(key="price", weight=2)], order=["stop"],
                              route=[{"place_name": "Сочи", "nights": 5}])
    child = TemplateFragment(constraints=[ConstraintItem(kind="min_stars", value=5)], remove=["breakfast"],
                             priorities=[PriorityItem(key="location")])
    r = resolve(child, parent)
    assert [(c.kind, c.value) for c in r.fragment.constraints] == [("min_stars", 5)]
    assert {p.key_ for p in r.fragment.priorities} == {"price", "location"}
    assert [s.place_name for s in r.fragment.route] == ["Сочи"] and r.provenance["route"] == "parent"
    assert r.provenance["constraint:min_stars"] == "child" and r.provenance["priority:price"] == "parent"
    assert "constraint:breakfast" not in r.provenance


def test_a_child_with_its_own_route_replaces_the_parents_route_whole():
    parent = TemplateFragment(route=[{"place_name": "A", "nights": 1}, {"place_name": "B", "nights": 2}], order=["stop", "stop"])
    child = TemplateFragment(route=[{"place_name": "C", "nights": 3}], order=["stop"])
    assert [s.place_name for s in resolve(child, parent).fragment.route] == ["C"]


def test_instantiate_lays_dates_out_from_the_start_and_carries_no_offers():
    draft = instantiate(skeleton_from_state(JAPAN), "Токио и Осака", date(2027, 3, 10))
    nodes = [draft["nodes"][n] for n in draft["sequence"]]
    stays = [n for n in nodes if n["type"] == "stay"]
    assert [(n["check_in"], n["check_out"]) for n in stays] == [("2027-03-10", "2027-03-14"), ("2027-03-14", "2027-03-18")]
    assert nodes[-1]["departure_date"] == "2027-03-18" and nodes[-1]["mode"] == "flight"
    assert all(n["selected_offer_ref"] is None and n["status"] == "empty" for n in nodes)
    assert draft["offers"] == {} and draft["status"] == "draft" and draft["title"] == "Как в «Токио и Осака»"
    assert draft["route"] == [{"place": "токио", "nights": 4}, {"place": "осака", "nights": 4}]


def test_instantiate_without_a_start_date_leaves_dates_empty():
    draft = instantiate(skeleton_from_state(JAPAN), "Токио и Осака")
    assert all(not (n.get("check_in") or n.get("departure_date")) for n in draft["nodes"].values())


def test_instantiate_applies_the_templates_criteria_with_provenance():
    frag = TemplateFragment(route=[{"place_name": "Сочи", "nights": 4}], order=["stop"],
                            constraints=[ConstraintItem(kind="breakfast")], priorities=[PriorityItem(key="location")])
    draft = instantiate(frag, "Море")
    assert draft["constraints"][0]["source"] == {"origin": "history", "quote": "Из шаблона «Море»"}
    assert draft["priorities"][0]["key"] == "location"


def test_a_bad_meal_means_the_traveller_wants_good_food_never_that_they_dislike_breakfast():
    # breakfast wasn't included: they want it next time
    got = extract(stay(breakfast=False), -1, {"meal": -1}, ev())
    items = {c.item.key: c.item for c in got}
    assert items["priority:meal"].weight == 2.0 and items["constraint:breakfast"].value is True
    # it was included but poor: quality matters, which a "breakfast" requirement would not fix
    got = extract(stay(breakfast=True), -1, {"meal": -1}, ev())
    assert {c.item.key for c in got} == {"hotel:avoid:h1", "priority:meal"}
    # nothing ever says "no breakfast"
    assert all(not (isinstance(c.item, ConstraintItem) and c.item.value is False) for c in got)


def test_bad_cancellation_conditions_mean_the_traveller_wants_free_cancellation():
    got = {c.item.key: c.item for c in extract(stay(), -1, {"cancellation": -1}, ev())}
    assert got["constraint:free_cancellation"].value is True
