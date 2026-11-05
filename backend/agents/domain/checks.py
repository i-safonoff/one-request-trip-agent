"""Verifier rules: do the pieces of the trip fit together? Pure code over the selected offers.

All times are tz-aware; every comparison happens on local time at the place in question
(`Place.tz`, exact for catalog cities, nearest-city estimate otherwise).
Codes are stable: the UI and the answer templates key off them.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from .budget import compute_pricing
from .geo import haversine_km, to_local
from .models import Check, Node, Offer, TripState

BUFFER_MIN = {"flight_domestic": 120, "flight_international": 180, "rail": 30, "bus": 20, "drive": 0}
MIN_CONNECTION_MIN = {"flight_domestic": 60, "flight_international": 90}


def _sel(s: TripState, n: Node) -> Offer | None:
    return s.offers.get(n.selected_offer_ref) if n.selected_offer_ref else None


def _tz(s: TripState, pid: str | None) -> str | None:
    p = s.places.get(pid or "")
    return p.tz if p else None


def _hhmm(v: str | None, default: str) -> time:
    try:
        h, m = (v or default).split(":")[:2]
        return time(int(h), int(m))
    except ValueError:
        h, m = default.split(":")
        return time(int(h), int(m))


def _international(s: TripState, node: Node) -> bool:
    a, b = s.places.get(node.from_ or ""), s.places.get(node.to or "")
    return bool(a and b and a.country and b.country and a.country != b.country)


def _buffer(s: TripState, node: Node, mode: str) -> int:
    if mode == "flight":
        return BUFFER_MIN["flight_international" if _international(s, node) else "flight_domestic"]
    return BUFFER_MIN.get(mode, 30)


def _transfer_minutes(s: TripState, idx: int, step: int) -> int:
    j = idx + step
    seq = s.sequence
    if 0 <= j < len(seq) and s.nodes[seq[j]].type == "transfer":
        return int(s.nodes[seq[j]].attrs.get("duration_min") or 0)
    return 0


def _has_transfer(s: TripState, idx: int, step: int) -> bool:
    j = idx + step
    return 0 <= j < len(s.sequence) and s.nodes[s.sequence[j]].type == "transfer"


def run_checks(state: TripState, now: datetime | None = None, *, strict_stale: bool = False) -> list[Check]:
    now = now or datetime.now(timezone.utc)
    out: list[Check] = []
    seq = state.sequence
    nodes = [state.nodes[i] for i in seq]

    def add(code, severity, node_ids, suggestion=None, **facts):
        out.append(Check(code=code, severity=severity, node_ids=list(node_ids), facts=facts, suggestion=suggestion))

    # --- search state
    for n in nodes:
        if n.type == "transfer":
            continue
        if n.status == "not_found":
            add("NODE_NOT_FOUND", "error", [n.id], "Смягчить условия или изменить даты", node=n.id)
        elif n.status == "stale":  # dates, party or filters changed and the new search did not happen or failed
            add("NODE_UNSEARCHED", "error", [n.id], "Повторить поиск по этому участку", node=n.id, status=n.status)
        elif n.status in ("empty", "searching"):
            add("NODE_UNSEARCHED", "warning", [n.id], "Дождаться поиска", node=n.id, status=n.status)

    # --- per transport / stay pairs in sequence order
    for i, n in enumerate(nodes):
        off = _sel(state, n)
        if n.type == "transport" and off and off.transport:
            t = off.transport
            first, last = t.segments[0], t.segments[-1]
            dep_tz, arr_tz = _tz(state, n.from_), _tz(state, n.to)
            dep_l, arr_l = to_local(first.departure, dep_tz), to_local(last.arrival, arr_tz)

            # CONNECTION_TIME
            for a, b in zip(t.segments, t.segments[1:]):
                gap = (b.departure - a.arrival).total_seconds() / 60
                key = "flight_international" if _international(state, n) else "flight_domestic"
                need = MIN_CONNECTION_MIN.get(key, 30) if a.mode == "flight" else 15
                same_hub = a.destination_name == b.origin_name
                if not same_hub:
                    need += 90
                if gap < need:
                    add("CONNECTION_TIME", "error", [n.id], "Выбрать вариант с более долгой пересадкой",
                        gap_min=round(gap), need_min=need, at=a.destination_name)

            # TZ_DATE_SHIFT
            if arr_l.date() != dep_l.date() and t.duration_min < 24 * 60:
                add("TZ_DATE_SHIFT", "info", [n.id], "Проверьте даты проживания",
                    departure=dep_l.isoformat(), arrival_local=arr_l.isoformat())

            # next stay: arrival-related rules
            stay_idx = next((j for j in range(i + 1, len(nodes)) if nodes[j].type != "transfer"), None)
            if stay_idx is not None and nodes[stay_idx].type == "stay":
                st = nodes[stay_idx]
                so = _sel(state, st)
                place = state.places.get(st.place or "")
                # DATES_STAY_ARRIVAL
                if st.check_in:
                    if arr_l.date() > st.check_in:
                        add("DATES_STAY_ARRIVAL", "error", [n.id, st.id], "Сдвинуть заселение на день прибытия",
                            arrival_date=str(arr_l.date()), check_in=str(st.check_in))
                    elif arr_l.date() < st.check_in:
                        add("DATES_STAY_ARRIVAL", "warning", [n.id, st.id], "Добавить ночь или сдвинуть билет",
                            arrival_date=str(arr_l.date()), check_in=str(st.check_in))
                # PLACE_CHAIN
                if place and place.lat is not None and last.destination_lat is not None:
                    d = haversine_km(place.lat, place.lon, last.destination_lat, last.destination_lon)
                    if d > 60:
                        add("PLACE_CHAIN", "error", [n.id, st.id], "Выбрать билет до нужного города",
                            distance_km=round(d), arrives=last.destination_name)
                # transfer / hotel arrival timing
                tr_min = _transfer_minutes(state, i, +1)
                hotel = so.hotel if so else None
                if hotel and hotel.lat is not None and last.destination_lat is not None:
                    d = haversine_km(hotel.lat, hotel.lon, last.destination_lat, last.destination_lon)
                    if d > 3 and not _has_transfer(state, i, +1):
                        add("MISSING_TRANSFER", "warning", [n.id, st.id], "Добавить трансфер",
                            distance_km=round(d, 1), arrives=last.destination_name)
                ready = arr_l + timedelta(minutes=tr_min)
                if so and hotel and st.check_in and ready.date() == st.check_in:
                    ci = _hhmm(hotel.check_in_time, "14:00")
                    wait_h = (datetime.combine(ready.date(), ci) - ready.replace(tzinfo=None)).total_seconds() / 3600
                    if wait_h > 3:
                        add("EARLY_ARRIVAL", "warning", [n.id, st.id], "Запросить ранний заезд или сдать багаж на хранение",
                            arrival_local=ready.isoformat(), check_in_time=ci.strftime("%H:%M"), wait_hours=round(wait_h, 1))
                    if ready.hour < 6:
                        sev = "info" if hotel.front_desk_24h else "warning"
                        add("NIGHT_ARRIVAL", sev, [n.id, st.id], "Предупредить отель о ночном заезде",
                            arrival_local=ready.isoformat(), front_desk_24h=hotel.front_desk_24h)
                    elif ready.hour >= 23 and not hotel.front_desk_24h:
                        add("LATE_CHECKIN", "warning", [n.id, st.id], "Уточнить у отеля поздний заезд",
                            arrival_local=ready.isoformat(), front_desk_24h=hotel.front_desk_24h)

            # previous stay: departure-related rules
            prev_idx = next((j for j in range(i - 1, -1, -1) if nodes[j].type != "transfer"), None)
            if prev_idx is not None and nodes[prev_idx].type == "stay":
                st = nodes[prev_idx]
                so = _sel(state, st)
                if st.check_out:
                    if dep_l.date() < st.check_out:
                        add("DATES_STAY_DEPARTURE", "error", [st.id, n.id], "Выезд раньше выезда из отеля: сдвинуть даты",
                            departure_date=str(dep_l.date()), check_out=str(st.check_out))
                    elif dep_l.date() > st.check_out:
                        add("DATES_STAY_DEPARTURE", "warning", [st.id, n.id], "Между выездом из отеля и отправлением есть день без жилья",
                            departure_date=str(dep_l.date()), check_out=str(st.check_out))
                tr = _transfer_minutes(state, i, -1)
                leave = dep_l - timedelta(minutes=_buffer(state, n, t.mode) + tr)
                if t.mode in ("flight", "rail", "bus") and leave.hour < 5 and leave.date() == dep_l.date():
                    add("DEPARTURE_BUFFER", "warning", [st.id, n.id], "Заказать ранний трансфер",
                        leave_hotel_at=leave.isoformat(), buffer_min=_buffer(state, n, t.mode), transfer_min=tr)
                if so and so.hotel and dep_l.date() == st.check_out:
                    co = _hhmm(so.hotel.check_out_time, "12:00")
                    gap_h = (leave.replace(tzinfo=None) - datetime.combine(dep_l.date(), co)).total_seconds() / 3600
                    if gap_h > 4:
                        add("LATE_DEPARTURE_GAP", "info", [st.id, n.id], "Попросить поздний выезд или оставить багаж",
                            hours_between=round(gap_h, 1), check_out_time=co.strftime("%H:%M"))

    # --- NIGHTS_COVERAGE between consecutive stays
    stays = sorted((n for n in nodes if n.type == "stay" and n.check_in and n.check_out), key=lambda n: n.check_in)
    for a, b in zip(stays, stays[1:]):
        if b.check_in < a.check_out:
            add("NIGHTS_COVERAGE", "error", [a.id, b.id], "Проживания пересекаются", overlap_nights=(a.check_out - b.check_in).days)
        elif b.check_in > a.check_out and not any(x.type == "transport" for x in nodes):
            add("NIGHTS_COVERAGE", "warning", [a.id, b.id], "Есть ночи без проживания", gap_nights=(b.check_in - a.check_out).days)

    # --- the stays must cover the whole requested period
    stays = [n for n in nodes if n.type == "stay" and n.check_in and n.check_out]
    if stays and state.date_from and state.date_to and (state.scope or "full") in ("full", "stay"):
        first, last = stays[0], stays[-1]
        if first.check_in != state.date_from or last.check_out != state.date_to:
            key = f"TRIP_DATES_MISMATCH:{first.check_in.isoformat()}:{last.check_out.isoformat()}"
            # the user can knowingly accept the shifted dates (a long flight lands later): then it is a warning, not a blocker
            add("TRIP_DATES_MISMATCH", "warning" if key in state.accepted_checks else "error", [first.id, last.id],
                "Принять новые даты проживания или изменить запрос", key=key, accepted=key in state.accepted_checks,
                requested_from=state.date_from.isoformat(), requested_to=state.date_to.isoformat(),
                stays_from=first.check_in.isoformat(), stays_to=last.check_out.isoformat())

    # --- offers: constraints, stale, cancellation, capacity
    for n in nodes:
        o = _sel(state, n)
        if not o:
            continue
        sc = n.scores.get(o.id)
        hard = {c.kind for c in state.hard()}
        if sc:
            for kind, status in sc.constraints.items():
                if kind in hard and status == "fail":
                    add("CONSTRAINT_FAIL", "error", [n.id], "Выбрать другой вариант или смягчить условие", kind=kind)
                elif kind in hard and status == "unknown":
                    add("CONSTRAINT_UNKNOWN", "warning", [n.id], "Источник не подтверждает условие: проверьте вручную", kind=kind)
        age_min = (now - o.retrieved_at).total_seconds() / 60
        if age_min > 30:
            add("OFFER_STALE", "error" if strict_stale else "info", [n.id], "Перепроверить цену", age_min=round(age_min))
        if o.rate and o.rate.free_cancellation_until and o.rate.free_cancellation_until < now:
            add("CANCELLATION_EXPIRED", "warning", [n.id], "Срок бесплатной отмены прошёл",
                until=o.rate.free_cancellation_until.isoformat())
        if o.rate and o.rate.max_occupancy and state.party.total > o.rate.max_occupancy:
            add("PARTY_CAPACITY", "error", [n.id], "Выбрать номер вместимостью больше",
                party=state.party.total, max_occupancy=o.rate.max_occupancy)
    if state.party.children_unknown and any(n.type == "stay" for n in nodes):
        add("PARTY_CAPACITY", "error", [n.id for n in nodes if n.type == "stay"], "Нужен возраст ребёнка для расчёта",
            children_unknown=state.party.children_unknown)

    # --- BUDGET
    b = state.budget
    if b.amount is not None:
        p = compute_pricing(state)
        from decimal import Decimal
        scoped = Decimal(p["budget_scoped_total"])
        if scoped > b.amount:
            # a ceiling the agent derived from "сделай дешевле" (85% of the old total) is a goal, not the user's limit: missing it
            # is said in the answer but does not block the purchase
            severity = "warning" if b.source and b.source.origin == "derived" else "error"
            add("BUDGET", severity, [], "Смягчить условия или поднять бюджет", total=str(scoped),
                budget=str(b.amount), over=str(scoped - b.amount), scope=b.scope)
        elif p["missing"]:
            add("BUDGET", "warning", p["missing"], "Расчёт неполный: бюджет проверен только по найденному",
                known=p["known_subtotal"], missing=p["missing"])
    return out
