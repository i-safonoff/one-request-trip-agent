"""One test per verifier rule (docs/PLAN.md §6)."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from agents.domain.checks import run_checks
from agents.domain.models import (Budget, Constraint, HotelInfo, HotelRate, Money, Node, Offer, OptionScore, Party, Place,
                               Segment, TransportInfo, TripState)

MSK, SPB = ZoneInfo("Europe/Moscow"), ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def transport_offer(oid, dep, arr, mode="rail", segs=None, dest=(59.93, 30.33), orig=(55.76, 37.62), duration=None):
    segs = segs or [Segment(mode=mode, origin_name="A", origin_lat=orig[0], origin_lon=orig[1], destination_name="B",
                            destination_lat=dest[0], destination_lon=dest[1], departure=dep, arrival=arr)]
    dur = duration or int((segs[-1].arrival - segs[0].departure).total_seconds() / 60)
    return Offer(id=oid, kind="transport", source="mock", data_mode="mock", retrieved_at=NOW, price=Money(amount=Decimal(3000)),
                 transport=TransportInfo(mode=mode, segments=segs, duration_min=dur))


def hotel_offer(oid="h", ci="14:00", co="12:00", h24=True, lat=59.93, lon=30.33, price=10000, fc=True, until=None, max_occ=None):
    return Offer(id=oid, kind="hotel_rate", source="tutu_mcp", data_mode="live", retrieved_at=NOW, price=Money(amount=Decimal(price)),
                 hotel=HotelInfo(provider_hotel_id="1", name="Hotel", lat=lat, lon=lon, check_in_time=ci, check_out_time=co, front_desk_24h=h24),
                 rate=HotelRate(room_name="Std", breakfast_included=True, free_cancellation=fc, free_cancellation_until=until, max_occupancy=max_occ))


def make(out_dep, out_arr, ret_dep=None, ret_arr=None, hotel=None, check_in=date(2026, 11, 14), check_out=date(2026, 11, 16), **kw):
    s = TripState(places={"msk": Place(name="Москва", lat=55.76, lon=37.62, tz="Europe/Moscow", country="Россия"),
                          "spb": Place(name="СПб", lat=59.93, lon=30.33, tz="Europe/Moscow", country="Россия")},
                  party=Party(adults=2), origin="msk", **kw)
    s.nodes = {"outbound": Node(id="outbound", type="transport", from_="msk", to="spb", departure_date=out_dep.date(), status="selected", selected_offer_ref="o1"),
               "stay_spb": Node(id="stay_spb", type="stay", place="spb", check_in=check_in, check_out=check_out, status="selected", selected_offer_ref="h"),
               "return": Node(id="return", type="transport", from_="spb", to="msk", status="selected", selected_offer_ref="o2" if ret_dep else None)}
    s.sequence = ["outbound", "stay_spb", "return"]
    s.offers = {"o1": transport_offer("o1", out_dep, out_arr), "h": hotel or hotel_offer()}
    if ret_dep:
        s.offers["o2"] = transport_offer("o2", ret_dep, ret_arr, dest=(55.76, 37.62), orig=(59.93, 30.33))
    else:
        s.nodes["return"].status = "options"
    return s


def dt(day, hh, mm=0, tz=MSK):
    return datetime(2026, 11, day, hh, mm, tzinfo=tz)


def codes(s, **kw):
    return {c.code: c for c in run_checks(s, NOW, **kw)}


def test_clean_trip_has_no_errors():
    s = make(dt(14, 8), dt(14, 15), dt(16, 18), dt(17 - 1, 23))
    assert [c for c in run_checks(s, NOW) if c.severity == "error"] == []


def test_early_arrival_warns_with_wait_hours():
    c = codes(make(dt(14, 0, 30), dt(14, 7, 10)))["EARLY_ARRIVAL"]
    assert c.severity == "warning" and 6 < c.facts["wait_hours"] < 7.5


def test_night_arrival_depends_on_24h_desk():
    s = make(dt(13, 22), dt(14, 2, 30), hotel=hotel_offer(h24=False))
    assert codes(s)["NIGHT_ARRIVAL"].severity == "warning"
    s = make(dt(13, 22), dt(14, 2, 30), hotel=hotel_offer(h24=True))
    assert codes(s)["NIGHT_ARRIVAL"].severity == "info"


def test_late_checkin_without_24h_desk():
    assert "LATE_CHECKIN" in codes(make(dt(14, 16), dt(14, 23, 40), hotel=hotel_offer(h24=False)))


def test_stay_arrival_date_mismatch_is_error_when_arrival_is_later():
    # overnight train leaves on the 14th and arrives on the 15th, but check-in is the 14th
    c = codes(make(dt(14, 22), dt(15, 7)))["DATES_STAY_ARRIVAL"]
    assert c.severity == "error" and c.facts["arrival_date"] == "2026-11-15"


def test_arriving_a_day_early_is_only_a_warning():
    assert codes(make(dt(13, 20), dt(13, 23), check_in=date(2026, 11, 14)))["DATES_STAY_ARRIVAL"].severity == "warning"


def test_departure_before_checkout_is_error_after_is_warning():
    s = make(dt(14, 8), dt(14, 15), dt(15, 18), dt(15, 23))
    assert codes(s)["DATES_STAY_DEPARTURE"].severity == "error"
    s = make(dt(14, 8), dt(14, 15), dt(17, 18), dt(17, 23))
    assert codes(s)["DATES_STAY_DEPARTURE"].severity == "warning"


def test_very_early_departure_needs_buffer():
    s = make(dt(14, 8), dt(14, 15), dt(16, 6, 0), dt(16, 8))
    s.offers["o2"].transport.mode = "flight"
    c = codes(s)["DEPARTURE_BUFFER"]
    assert c.severity == "warning" and c.facts["buffer_min"] == 120


def test_late_departure_gap_is_info():
    s = make(dt(14, 8), dt(14, 15), dt(16, 21), dt(16, 23))
    c = codes(s)["LATE_DEPARTURE_GAP"]
    assert c.severity == "info" and c.facts["hours_between"] > 4


def test_connection_time_too_short():
    a = Segment(mode="flight", origin_name="X", destination_name="Hub", departure=dt(14, 6), arrival=dt(14, 8))
    b = Segment(mode="flight", origin_name="Hub", destination_name="B", destination_lat=59.93, destination_lon=30.33,
                departure=dt(14, 8, 40), arrival=dt(14, 10))  # 40 min < 60 domestic
    s = make(dt(14, 6), dt(14, 10))
    s.offers["o1"] = transport_offer("o1", dt(14, 6), dt(14, 10), mode="flight", segs=[a, b])
    c = codes(s)["CONNECTION_TIME"]
    assert c.severity == "error" and c.facts["gap_min"] == 40 and c.facts["need_min"] == 60


def test_tz_date_shift_is_flagged_for_cross_timezone_trips():
    tokyo = ZoneInfo("Asia/Tokyo")
    s = make(dt(14, 22), datetime(2026, 11, 15, 12, 0, tzinfo=tokyo))
    s.places["spb"] = Place(name="Токио", lat=35.68, lon=139.65, tz="Asia/Tokyo", country="Япония")
    s.offers["o1"] = transport_offer("o1", dt(14, 22), datetime(2026, 11, 15, 12, 0, tzinfo=tokyo), mode="flight", dest=(35.68, 139.65))
    s.offers["h"] = hotel_offer(lat=35.68, lon=139.65)
    assert codes(s)["TZ_DATE_SHIFT"].severity == "info"


def test_place_chain_wrong_destination():
    s = make(dt(14, 8), dt(14, 15))
    s.offers["o1"] = transport_offer("o1", dt(14, 8), dt(14, 15), dest=(56.30, 43.94))  # Nizhny, not St Petersburg
    assert codes(s)["PLACE_CHAIN"].severity == "error"


def test_missing_transfer_when_hub_is_far_from_hotel():
    s = make(dt(14, 8), dt(14, 15))
    s.offers["o1"] = transport_offer("o1", dt(14, 8), dt(14, 15), mode="flight", dest=(59.80, 30.26))  # airport ~15 km away
    assert "MISSING_TRANSFER" in codes(s)
    s.nodes["transfer_in"] = Node(id="transfer_in", type="transfer", from_="spb", to="spb", attrs={"duration_min": 40})
    s.sequence = ["outbound", "transfer_in", "stay_spb", "return"]
    assert "MISSING_TRANSFER" not in codes(s)


def test_stays_that_overlap_are_an_error():
    s = make(dt(14, 8), dt(14, 15))
    s.nodes["stay_x"] = Node(id="stay_x", type="stay", place="spb", check_in=date(2026, 11, 15), check_out=date(2026, 11, 18), status="options")
    s.sequence = ["outbound", "stay_spb", "stay_x", "return"]
    assert codes(s)["NIGHTS_COVERAGE"].severity == "error"


def test_party_capacity_and_unknown_child_age():
    s = make(dt(14, 8), dt(14, 15), hotel=hotel_offer(max_occ=1))
    assert codes(s)["PARTY_CAPACITY"].severity == "error"
    s = make(dt(14, 8), dt(14, 15))
    s.party = Party(adults=2, children_unknown=1)
    assert codes(s)["PARTY_CAPACITY"].facts["children_unknown"] == 1


def test_budget_error_and_incomplete_warning():
    s = make(dt(14, 8), dt(14, 15), dt(16, 18), dt(16, 23), budget=Budget(amount=Decimal(15000), scope="trip"))
    assert codes(s)["BUDGET"].severity == "error"
    s = make(dt(14, 8), dt(14, 15), budget=Budget(amount=Decimal(99000), scope="trip"))  # return is not selected
    assert codes(s)["BUDGET"].severity == "warning"


def test_cancellation_expired_and_stale_offer():
    past = NOW - timedelta(days=1)
    s = make(dt(14, 8), dt(14, 15), hotel=hotel_offer(until=past))
    assert "CANCELLATION_EXPIRED" in codes(s)
    s.offers["h"].retrieved_at = NOW - timedelta(minutes=45)
    assert codes(s)["OFFER_STALE"].severity == "info"
    assert codes(s, strict_stale=True)["OFFER_STALE"].severity == "error"  # at the cart stage it blocks payment


def test_unknown_and_failed_hard_constraints():
    s = make(dt(14, 8), dt(14, 15), constraints=[Constraint(id="pets_allowed", kind="pets_allowed", strength="hard")])
    s.nodes["stay_spb"].scores = {"h": OptionScore(offer_id="h", constraints={"pets_allowed": "unknown"})}
    assert codes(s)["CONSTRAINT_UNKNOWN"].severity == "warning"  # unknown is never silently a pass
    s.nodes["stay_spb"].scores = {"h": OptionScore(offer_id="h", constraints={"pets_allowed": "fail"})}
    assert codes(s)["CONSTRAINT_FAIL"].severity == "error"


def test_not_found_and_unsearched_nodes():
    s = make(dt(14, 8), dt(14, 15))
    s.nodes["stay_spb"].status = "not_found"
    s.nodes["return"].status = "stale"
    c = codes(s)
    assert c["NODE_NOT_FOUND"].severity == "error" and c["NODE_UNSEARCHED"].severity == "error"  # stale = the new search did not happen
    s.nodes["return"].status = "empty"
    assert codes(s)["NODE_UNSEARCHED"].severity == "warning"  # not searched yet is only a warning


# ---------------------------------------------------------------- arrival-aware dates (long-haul)
def test_check_in_moves_to_the_arrival_day_and_everything_after_shifts():
    from agents.domain.apply import apply_ops
    from agents.domain.graph import align_arrival_dates
    tokyo = ZoneInfo("Asia/Tokyo")
    s = make(dt(12, 20, 15), datetime(2026, 11, 13, 14, 0, tzinfo=tokyo), dt(16, 10), dt(16, 18),
             check_in=date(2026, 11, 12), check_out=date(2026, 11, 16))
    s.places["spb"].tz = "Asia/Tokyo"
    s.nodes["return"].departure_date = date(2026, 11, 16)  # real graphs always carry it
    ops = align_arrival_dates(s)
    s2 = apply_ops(s, "orchestrator", ops).state
    st, ret = s2.nodes["stay_spb"], s2.nodes["return"]
    assert (st.check_in, st.check_out) == (date(2026, 11, 13), date(2026, 11, 17))  # one day later, 4 nights kept
    assert ret.departure_date == date(2026, 11, 17)  # the way back moves with the stay
    assert align_arrival_dates(s2) == []  # idempotent
    assert s2.nodes["stay_spb"].status == "stale" or s2.nodes["stay_spb"].status == "selected"


def test_nothing_moves_when_arrival_fits():
    from agents.domain.graph import align_arrival_dates
    assert align_arrival_dates(make(dt(14, 8), dt(14, 15))) == []
