"""Defects found by the manual scenario run (scripts/manual_scenarios.py), each pinned by a test."""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from agents.domain.apply import apply_ops
from agents.domain.cartfp import cart_fingerprint
from agents.domain.models import (Cart, CartItem, HotelInfo, HotelRate, Money, Node, Offer, Place, Segment, TransportInfo,
                                  TripState)
from agents.domain.ops import SetDates, SetScope
from agents.runtime import aftersales
from agents.runtime.context import calendar_anchors
from agents.runtime.deltas import planner_ops
from agents.runtime.orchestrator import _fix_past_dates
from agents.runtime.schemas import PlannerOut, StopDelta


def test_calendar_anchors_give_weekdays_and_tomorrow():
    c = calendar_anchors(date(2026, 10, 28))  # Wednesday
    assert c["today_weekday"] == "среда"
    assert c["tomorrow"] == "2026-10-29"
    assert c["nearest_friday"] == "2026-10-30" and c["nearest_sunday"] == "2026-11-01"
    assert c["end_of_current_month"] == "2026-10-31"
    assert calendar_anchors(date(2026, 10, 30))["nearest_friday"] == "2026-11-06"  # strictly after today


def test_past_date_without_year_moves_to_next_year():
    p = PlannerOut(date_from=date(2026, 1, 10), date_to=date(2026, 1, 15))
    fixed = _fix_past_dates(p, "Сочи с 10 по 15 января", date(2026, 10, 28))
    assert fixed.date_from == date(2027, 1, 10) and fixed.date_to == date(2027, 1, 15)


def test_past_date_with_explicit_year_is_asked_not_booked():
    p = PlannerOut(date_from=date(2020, 1, 1), date_to=date(2020, 1, 5))
    fixed = _fix_past_dates(p, "отель с 1 по 5 января 2020 года", date(2026, 10, 28))
    assert fixed.date_from is None and fixed.date_to is None
    assert fixed.ambiguities and fixed.ambiguities[0].field == "dates"


def test_a_price_with_four_digits_is_not_taken_for_a_year():
    p = PlannerOut(date_from=date(2026, 1, 10), date_to=date(2026, 1, 15))
    fixed = _fix_past_dates(p, "Сочи с 10 по 15 января, до 20000", date(2026, 10, 28))
    assert fixed.date_from == date(2027, 1, 10)


def test_future_dates_are_untouched():
    p = PlannerOut(date_from=date(2026, 11, 6), date_to=date(2026, 11, 9))
    assert _fix_past_dates(p, "6-9 ноября", date(2026, 10, 28)) is p


def test_same_day_round_trip_only_without_a_stay():
    s = TripState()
    res = apply_ops(s, "planner", [SetScope(scope="transport"), SetDates(date_from=date(2026, 10, 29), date_to=date(2026, 10, 29))])
    assert res.state.date_from == res.state.date_to == date(2026, 10, 29)
    res = apply_ops(TripState(), "planner", [SetScope(scope="stay"), SetDates(date_from=date(2026, 10, 29), date_to=date(2026, 10, 29))])
    assert res.rejected  # a hotel stay needs at least a night


def test_origin_equal_to_destination_is_ignored():
    d = PlannerOut(intent="new_trip", scope="full", origin="Москва", stops=[StopDelta(name="Москва")])
    assert not [op for op in planner_ops(d, TripState(), None) if op.op == "set_origin"]
    d = PlannerOut(intent="new_trip", scope="full", origin="Казань", stops=[StopDelta(name="Москва")])
    assert [op for op in planner_ops(d, TripState(), None) if op.op == "set_origin"]


def _offer(amount):
    return Offer(id="o1", kind="hotel_rate", source="mock", data_mode="mock", retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                 price=Money(amount=amount), hotel=HotelInfo(provider_hotel_id="h", name="Отель"), rate=HotelRate(room_name="Стандарт"))


def test_cart_fingerprint_does_not_depend_on_decimal_scale():
    """The paid-order 409 bug: a price 2686.54 in memory and 2686.5400 after a round trip gave different fingerprints."""
    def state(amount):
        s = TripState()
        s.offers["o1"] = _offer(amount)
        s.nodes["n"] = Node(id="n", type="stay", status="selected", selected_offer_ref="o1", check_in=date(2026, 11, 1), check_out=date(2026, 11, 2))
        return s
    items = [CartItem(node_id="n", offer_id="o1", title="x", price=Money(amount=Decimal("1")))]
    assert cart_fingerprint(state(Decimal("2686.54")), items) == cart_fingerprint(state(Decimal("2686.540000")), items)
    assert cart_fingerprint(state(Decimal("2686.54")), items) != cart_fingerprint(state(Decimal("2686.55")), items)


def _booked_state() -> TripState:
    s = TripState()
    s.places["spb"] = Place(name="Санкт-Петербург", tz="Europe/Moscow")
    arr = datetime(2026, 11, 14, 5, 30, tzinfo=timezone.utc)
    s.offers["t1"] = Offer(id="t1", kind="transport", source="mock", data_mode="mock", retrieved_at=arr, price=Money(amount=Decimal("5000")),
                           transport=TransportInfo(mode="flight", segments=[Segment(
                               mode="flight", number="SU1", origin_name="A", destination_name="B", departure=datetime(2026, 11, 14, 4, 0, tzinfo=timezone.utc),
                               arrival=arr)]))
    s.offers["h1"] = _offer(Decimal("9000")).model_copy(update={"id": "h1", "hotel": HotelInfo(provider_hotel_id="h", name="Отель Нева", check_in_time="15:00"),
                                                              "rate": HotelRate(room_name="Стандарт", free_cancellation_until=datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc))})
    s.nodes["outbound"] = Node(id="outbound", type="transport", status="selected", selected_offer_ref="t1", departure_date=date(2026, 11, 14))
    s.nodes["stay"] = Node(id="stay", type="stay", status="selected", selected_offer_ref="h1", place="spb",
                           check_in=date(2026, 11, 14), check_out=date(2026, 11, 16))
    s.sequence = ["outbound", "stay"]
    s.cart = Cart(status="paid", order_number="EKV-TEST01")
    s.status = "booked"
    return s


def test_aftersales_detects_a_booked_plan_only():
    assert aftersales.is_booked(_booked_state())
    assert not aftersales.is_booked(TripState())


def test_early_checkin_request_names_hotel_arrival_and_the_order():
    text = aftersales.hotel_request(_booked_state(), "early")
    assert "EKV-TEST01" in text and "Отель Нева" in text and "08:30" in text and "15:00" in text
    assert "имитируются" in text  # never pretends the request was sent


def test_reminders_list_dates_in_order_and_do_not_promise_notifications():
    text = aftersales.reminders(_booked_state())
    assert text.index("последний срок бесплатной отмены") < text.index("заселение")
    assert "не отправляются" in text


def test_cancellation_note_lists_the_deadline():
    assert "бесплатная отмена до 11.11" in aftersales.cancellation_note(_booked_state())


@pytest.mark.parametrize("kind", ["early", "late"])
def test_request_without_stays_is_honest(kind):
    s = _booked_state()
    s.sequence = ["outbound"]
    assert "нет проживания" in aftersales.hotel_request(s, kind)


# ---- findings of the Telegram run (docs/telegram-test-report.md)
from agents.runtime.orchestrator import CHECKOUT_RE, _drop_unmentioned_dates  # noqa: E402
from agents.runtime.deltas import constraints_ops  # noqa: E402
from agents.runtime.schemas import ConstraintsOut  # noqa: E402


@pytest.mark.parametrize("text", ["Оформить", "оформить", "Оформляем!", "да, оформляем", "Купить", "Оформить поездку"])
def test_checkout_phrases_are_recognised(text):
    assert CHECKOUT_RE.match(text.strip())


@pytest.mark.parametrize("text", ["Оформить на 20 ноября", "оформи мне отель подешевле", "сделай дешевле", "а если на два дня позже?"])
def test_other_requests_are_not_checkout(text):
    assert not CHECKOUT_RE.match(text.strip())


def test_dates_without_a_date_word_are_dropped():
    """The model repeated "+2 days" from the history for a message that was only "Оформить"."""
    p = PlannerOut(shift_days=2)
    assert _drop_unmentioned_dates(p, "Оформить").shift_days is None
    assert _drop_unmentioned_dates(p, "а если на два дня позже?").shift_days == 2
    assert _drop_unmentioned_dates(p, "перенеси на 18 ноября").shift_days == 2


def test_a_dog_becomes_a_hard_pets_condition():
    ops = constraints_ops(ConstraintsOut(pets=["dog"]), TripState(), None)
    pets = [o for o in ops if o.op == "add_constraint" and o.kind == "pets_allowed"]
    assert pets and pets[0].strength == "hard"
    # not duplicated when the model already added it
    from agents.runtime.schemas import ConstraintIn
    ops = constraints_ops(ConstraintsOut(pets=["dog"], add=[ConstraintIn(kind="pets_allowed")]), TripState(), None)
    assert len([o for o in ops if o.op == "add_constraint" and o.kind == "pets_allowed"]) == 1


# ---- findings of the cross-surface run (fix plan, stages 1-3)
from datetime import timedelta  # noqa: E402

from agents.domain import fmt  # noqa: E402
from agents.domain.budget import compute_pricing  # noqa: E402
from agents.domain.ops import SetRoute  # noqa: E402
from agents.domain.turn import contradicted_claims  # noqa: E402
from agents.runtime.deltas import budget_ops  # noqa: E402
from agents.runtime.orchestrator import (Orchestrator, _fix_weekend, _keep_scope, _mentions_dates,  # noqa: E402
                                         _vague_destination)
from agents.runtime.schemas import BudgetOut  # noqa: E402


@pytest.mark.parametrize("text, expected", [
    ("до 5000 за всё", False), ("до 30 000 рублей на каждого", False), ("30к", False), ("бюджет 450 000", False),
    ("нас будет четверо", False), ("Оформить", False), ("сделай дешевле", False),
    ("а если на два дня позже?", True), ("с 14 по 16", True), ("14.11-16.11", True), ("на 3 дня", True),
    ("завтра", True), ("на выходные", True), ("15 ноября", True), ("через неделю", True)])
def test_money_is_not_a_date(text, expected):
    assert _mentions_dates(text) is expected


def _trip(scope="full"):
    s = TripState()
    s.scope = scope
    s.route = [type("R", (), {"place": "spb", "nights": None})()]
    return s


def test_a_small_change_does_not_narrow_the_trip_to_a_hotel_search():
    p = PlannerOut(scope="stay")
    assert _keep_scope(p, "завтрак больше не нужен", _trip()).scope is None
    assert _keep_scope(p, "без билетов, только отель", _trip()).scope == "stay"
    assert _keep_scope(p, "билеты не нужны", _trip()).scope == "stay"
    assert _keep_scope(PlannerOut(scope="full"), "хочу в Казань", TripState()).scope == "full"  # a new request is left alone


@pytest.mark.parametrize("given, text, want", [
    (date(2026, 11, 4), "через неделю на выходные", (date(2026, 11, 6), date(2026, 11, 8))),  # Wed -> Fri..Sun
    (date(2026, 11, 7), "на выходные", (date(2026, 11, 7), date(2026, 11, 8))),               # Sat..Sun stays
    (date(2026, 11, 6), "на выходные", (date(2026, 11, 6), date(2026, 11, 8)))])
def test_weekend_lands_on_friday_to_sunday(given, text, want):
    p = _fix_weekend(PlannerOut(date_from=given, date_to=given + timedelta(days=1)), text)
    assert (p.date_from, p.date_to) == want


def test_explicit_dates_win_over_the_word_weekend():
    p = PlannerOut(date_from=date(2026, 11, 4), date_to=date(2026, 11, 5))
    assert _fix_weekend(p, "на выходные 4-5 ноября") is p


def test_today_is_the_users_day_not_utc(monkeypatch):
    class S:  # settings stub
        user_tz = "Europe/Moscow"
    o = Orchestrator.__new__(Orchestrator)
    o.now = lambda: datetime(2026, 10, 28, 21, 49, tzinfo=timezone.utc)  # 00:49 on 29 October in Moscow
    o.tz = __import__("zoneinfo").ZoneInfo(S.user_tz)
    assert o.today() == date(2026, 10, 29)


def _cities_without_origin():
    s = TripState()
    res = apply_ops(s, "planner", [SetScope(scope="full"), SetRoute(stops=[{"name": "Москва"}, {"name": "Казань"}, {"name": "Нижний Новгород"}]),
                                   SetDates(date_from=date(2026, 11, 1), date_to=date(2026, 11, 8))])
    return res.state


def test_moves_between_cities_exist_without_an_origin_and_the_plan_is_not_complete():
    s = _cities_without_origin()
    kinds = [s.nodes[i].type for i in s.sequence]
    assert kinds.count("transport") == 2 and "outbound" not in s.nodes and "return" not in s.nodes
    # even if everything that exists is priced, the tickets to and from the trip are still missing
    s.origin = None
    assert compute_pricing(s)["needs_origin"] is True and compute_pricing(s)["is_complete"] is False


def test_a_direct_flight_claim_over_a_transfer_is_rejected():
    s = _booked_state()
    s.offers["t1"].transport.transfers = 1
    assert contradicted_claims("Перелёты: прямые рейсы туда-обратно", s)
    s.offers["t1"].transport.transfers = 0
    assert not contradicted_claims("Перелёты: прямые рейсы туда-обратно", s)


def test_everything_fits_next_to_a_failed_check_is_rejected():
    from agents.domain.models import Check
    s = _booked_state()
    s.checks = [Check(code="TRIP_DATES_MISMATCH", severity="error")]
    assert contradicted_claims("Всё соответствует вашему запросу.", s)
    s.checks = []
    assert not contradicted_claims("Всё соответствует вашему запросу.", s)


def test_per_person_budget_is_multiplied_by_the_party():
    s = TripState()
    s.party.adults = 2
    ops = budget_ops(BudgetOut(amount=Decimal("30000"), per_person=True), s, None, None)
    assert [o for o in ops if o.op == "set_budget"][0].amount == Decimal("60000")
    ops = budget_ops(BudgetOut(amount=Decimal("30000")), s, None, None)
    assert [o for o in ops if o.op == "set_budget"][0].amount == Decimal("30000")


def test_a_mood_word_starts_the_destination_step():
    p = _vague_destination(PlannerOut(), "Куда поехать кататься на лыжах в январе", TripState())
    assert p.destination_vague and p.intent == "new_trip"
    assert _vague_destination(PlannerOut(), "Казань на выходные", TripState()).destination_vague is None


def test_raw_decimal_money_in_the_text_gets_the_russian_format():
    assert fmt.fix_money("Итого 3315.86 ₽, номер 1 590 ₽, рейтинг 8.5/10") == f"Итого 3{fmt.NBSP}316 ₽, номер 1 590 ₽, рейтинг 8.5/10"


def test_the_tunnel_address_file_overrides_the_public_url(tmp_path):
    from shared.config import Settings, refresh_public_url
    f = tmp_path / "public_url"
    s = Settings(public_url_file=str(f), public_base_url="http://localhost:8080")
    assert refresh_public_url(s) is False  # no file yet: nothing changes
    f.write_text("https://abc123.lhr.life\n", "utf-8")
    assert refresh_public_url(s) is True and s.public_base_url == "https://abc123.lhr.life"
    assert refresh_public_url(s) is False  # same address: no change
    f.write_text("http://not-https.example", "utf-8")
    assert refresh_public_url(s) is False and s.public_base_url == "https://abc123.lhr.life"  # only https is accepted
    f.write_text("https://def456.lhr.life", "utf-8")
    assert refresh_public_url(s) is True and s.public_base_url == "https://def456.lhr.life"


# ---- "Окей, хочу купить": purchase wording, shifted dates, budget echo
from agents.runtime.orchestrator import ACCEPT_DATES_RE, CHECKOUT_RE as _CK, _drop_unmentioned_budget  # noqa: E402
from agents.domain.checks import run_checks  # noqa: E402
from agents.domain.ops import AcceptChecks  # noqa: E402


@pytest.mark.parametrize("text", ["Окей. я хочу купить", "Я согласе с планом, как оформить заказ", "Я согласен с планом, как оформить заказ",
                                  "хочу купить", "как мне оформить?", "Оформить", "да, оформляем"])
def test_purchase_phrases(text):
    assert _CK.search(text.strip())


@pytest.mark.parametrize("text", ["сделай дешевле", "оформи мне отель подешевле в центре", "а если на два дня позже?"])
def test_not_purchase_phrases(text):
    assert not _CK.search(text.strip())


def test_accepting_shifted_dates_turns_the_blocker_into_a_warning():
    s = _booked_state()
    s.date_from, s.date_to = date(2026, 11, 12), date(2026, 11, 14)  # asked for 12-14, the stay is 14-16
    err = [c for c in run_checks(s) if c.code == "TRIP_DATES_MISMATCH"]
    assert err and err[0].severity == "error"
    res = apply_ops(s, "user", [AcceptChecks(keys=[err[0].facts["key"]])])
    again = [c for c in run_checks(res.state) if c.code == "TRIP_DATES_MISMATCH"]
    assert again and again[0].severity == "warning"
    # new dates are a new question: the acceptance does not carry over
    moved = res.state.model_copy(deep=True)
    moved.nodes["stay"].check_in = date(2026, 11, 15)
    assert [c for c in run_checks(moved) if c.code == "TRIP_DATES_MISMATCH"][0].severity == "error"


def test_accept_phrase():
    assert ACCEPT_DATES_RE.search("принимаю даты") and ACCEPT_DATES_RE.search("Согласен с датами")
    assert not ACCEPT_DATES_RE.search("сделай дешевле")


def test_an_amount_without_money_words_is_dropped():
    b = BudgetOut(amount=Decimal("400000"), per_person=True)
    assert _drop_unmentioned_budget(b, "Окей. я хочу купить").amount is None
    assert _drop_unmentioned_budget(b, "бюджет 200 000 с каждого") is b
    assert _drop_unmentioned_budget(b, "до 5000 за всё") is b


def test_a_flight_without_airport_coordinates_is_still_an_arc():
    from agents.domain.view import _geometry
    s = _booked_state()
    s.places["msk"] = Place(name="Москва", lat=55.75, lon=37.62)
    s.places["spb"] = Place(name="Санкт-Петербург", lat=59.93, lon=30.33, tz="Europe/Moscow")
    for seg in s.offers["t1"].transport.segments:
        seg.origin_lat = seg.destination_lat = None  # hubs were not geocoded
    n = s.nodes["outbound"]
    n.from_, n.to = "msk", "spb"
    geom, status = _geometry(s, n)
    assert status == "great_circle" and len(geom["coordinates"]) == 2
    s.offers["t1"].transport.mode = "rail"
    assert _geometry(s, n)[1] == "straight"
