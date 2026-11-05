"""Ranking and comparison of offers for one node. Pure code: the LLM never computes scores.

1. Hard constraints are evaluated per offer as pass / fail / unknown. Unknown is NOT a pass.
2. Criteria are normalised to 0..1 across the node's options.
3. score = sum(weight * criterion). Weights start from defaults and are shaped by the user's
   priorities ("недорого" -> price up, "без долгих перелётов" -> duration up).
4. Trade-offs against the nearest alternatives are returned as structured facts, which is what
   the main agent's "чем пожертвовали" is written from.
"""
from __future__ import annotations


from . import fmt
from .geo import haversine_km
from . import hotel_fit
from .models import CheckStatus, Constraint, Node, Offer, OptionScore, TripState

DEFAULT_WEIGHTS = {
    "stay": {"price": 0.35, "rating": 0.25, "location": 0.15, "cancellation": 0.15, "meal": 0.10},
    "transport": {"price": 0.35, "duration": 0.30, "transfers": 0.15, "schedule": 0.20},
}
PRIORITY_KEYS = {"price", "rating", "location", "cancellation", "meal", "duration", "transfers", "schedule", "stars"}
PRIORITY_ALIAS = {"travel_time": "duration", "time": "duration", "comfort": "stars", "cost": "price"}

# bed_type / view are the agents' own additions (the history module does not inherit them): a preference for the room
STAY_KINDS = {"breakfast", "free_cancellation", "pets_allowed", "kid_friendly", "min_stars", "min_rating", "bed_type", "view"}
TRANSPORT_KINDS = {"max_travel_hours", "max_transfers", "exclude_modes", "only_modes", "avoid_overnight"}


def _applies(kind: str, node: Node) -> bool:
    if kind in STAY_KINDS:
        return node.type == "stay"
    if kind in TRANSPORT_KINDS:
        return node.type == "transport"
    return True


def _tri(v: bool | None) -> CheckStatus:
    return "unknown" if v is None else ("pass" if v else "fail")


def eval_constraint(c: Constraint, offer: Offer) -> CheckStatus:
    h, r, t = offer.hotel, offer.rate, offer.transport
    k = c.kind
    if k == "breakfast" and r:
        return _tri(r.breakfast_included)
    if k == "free_cancellation" and r:
        return _tri(r.free_cancellation)
    if k == "pets_allowed" and h:
        return _tri(h.pets_allowed)
    if k == "kid_friendly" and h:
        return _tri(h.kid_friendly)
    if k == "bed_type" and r:
        return "unknown" if r.bed_type is None else _tri(r.bed_type == c.value)
    if k == "view" and r:
        if r.view is None or (c.value == "sea" and r.view == "water"):
            return "unknown"  # the source did not say, or only "on the water" (a pond is not the sea)
        return _tri(r.view == c.value)
    if k == "min_stars" and h:
        return "unknown" if h.stars is None else _tri(h.stars >= float(c.value))
    if k == "min_rating" and h:
        return "unknown" if h.rating is None else _tri(h.rating >= float(c.value))
    if k == "max_travel_hours" and t:
        return _tri(t.duration_min <= float(c.value) * 60)
    if k == "max_transfers" and t:
        return _tri(t.transfers <= int(c.value))
    if k == "exclude_modes" and t:
        return _tri(t.mode not in (c.value if isinstance(c.value, list) else [c.value]))
    if k == "only_modes" and t:
        return _tri(t.mode in (c.value if isinstance(c.value, list) else [c.value]))
    if k == "avoid_overnight" and t:
        return _tri(not t.overnight)
    return "unknown"


def evaluate(state: TripState, node: Node, offer: Offer) -> dict[str, CheckStatus]:
    return {c.kind: eval_constraint(c, offer) for c in state.constraints if _applies(c.kind, node)}


def _ratio_inv(values: list[float], v: float) -> float:
    """Cheapest / shortest = 1.0, twice as expensive = 0.5. (Min-max would turn two options into 0 or 1
    and let a small price gap beat any priority.)"""
    lo = min(values)
    return 1.0 if v <= 0 or lo <= 0 else min(1.0, lo / v)


def _rating(h) -> float | None:
    if h is None or h.rating is None:
        return None
    n = h.review_count or 0
    return ((h.rating * n + 7.5 * 20) / (n + 20)) / 10  # shrink towards 7.5 for thin review counts


def _cancellation(offer: Offer, node: Node) -> float:
    r = offer.rate
    if r is None or r.free_cancellation is None:
        return 0.3
    if not r.free_cancellation:
        return 0.0
    if r.free_cancellation_until and node.check_in:
        slack = (node.check_in - r.free_cancellation_until.date()).days
        return 0.8 + 0.2 / (1 + max(slack - 1, 0) / 2)
    return 0.8


def _meal(offer: Offer) -> float:
    r = offer.rate
    if r is None or r.meal is None and r.breakfast_included is None:
        return 0.3
    return 1.0 if (r.breakfast_included or r.meal in ("breakfast", "half_board", "full_board", "all_inclusive")) else 0.0


def _schedule(offer: Offer, tz: str | None) -> float:
    seg = offer.transport.segments
    from .geo import to_local

    def hour_score(h: int, kind: str) -> float:
        if kind == "dep":
            return 0.2 if h < 5 else 0.6 if h < 7 else 1.0 if h < 22 else 0.5
        return 0.3 if h < 6 else 1.0 if h < 23 else 0.6
    dep = hour_score(to_local(seg[0].departure, tz).hour, "dep")
    arr = hour_score(to_local(seg[-1].arrival, tz).hour, "arr")
    return (dep + arr) / 2


def history_hint(state: TripState, offer: Offer) -> str | None:
    """'like' / 'avoid' if the user said so about this hotel on a past trip (matched by provider id or name)."""
    h = offer.hotel
    if not h or not state.history_hints:
        return None
    for hint in state.history_hints:
        same = (hint.get("provider_hotel_id") and hint["provider_hotel_id"] == h.provider_hotel_id) or \
               (hint.get("name") and hint["name"].strip().lower() == h.name.strip().lower())
        if same:
            return hint.get("polarity")
    return None


def weights_for(state: TripState, node_type: str) -> dict[str, float]:
    w = dict(DEFAULT_WEIGHTS["transport" if node_type == "transport" else "stay"])
    for p in state.priorities:
        key = PRIORITY_ALIAS.get(p.key, p.key)
        if key in w:
            w[key] *= 1 + p.weight
        elif key == "stars" and node_type == "stay":
            w["stars"] = 0.15 * (1 + p.weight)
    total = sum(w.values())
    return {k: v / total for k, v in w.items()}


def criteria_for(state: TripState, node: Node, offers: list[Offer]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {o.id: {} for o in offers}
    if not offers:
        return out
    prices = [float(o.price.amount) for o in offers]
    for o, p in zip(offers, prices):
        out[o.id]["price"] = _ratio_inv(prices, p)
    if node.type == "stay":
        center = state.places.get(node.place or "")
        for o in offers:
            c, h = out[o.id], o.hotel
            rt = _rating(h)
            c["rating"] = rt if rt is not None else 0.5
            if center and center.lat is not None and h and h.lat is not None:
                c["location"] = 1 / (1 + haversine_km(center.lat, center.lon, h.lat, h.lon) / 4)
            else:
                c["location"] = 0.5
            c["cancellation"] = _cancellation(o, node)
            c["meal"] = _meal(o)
            c["stars"] = (h.stars or 0) / 5 if h else 0.0
    else:
        tz = state.places[node.from_].tz if node.from_ in state.places else None
        durs = [float(o.transport.duration_min) for o in offers]
        for o, d in zip(offers, durs):
            c = out[o.id]
            c["duration"] = _ratio_inv(durs, d)
            c["transfers"] = 1 / (1 + o.transport.transfers)
            c["schedule"] = _schedule(o, tz)
    return out


def rank_node(state: TripState, node: Node) -> tuple[dict[str, OptionScore], str | None]:
    """(scores for every option, id to select). Selection prefers offers whose hard constraints
    all pass, then ones with unknowns; offers that fail a hard constraint are never selected."""
    offers = [state.offers[i] for i in node.option_ids if i in state.offers and i not in node.excluded_offers]
    if not offers:
        return {}, None
    crit = criteria_for(state, node, offers)
    weights = weights_for(state, node.type)
    scores: dict[str, OptionScore] = {}
    for o in offers:
        cons = evaluate(state, node, o)
        hard_kinds = {c.kind for c in state.hard()}
        hard_vals = [v for k, v in cons.items() if k in hard_kinds]
        total = sum(weights.get(k, 0.0) * v for k, v in crit[o.id].items())
        risk = hotel_fit.disappointment(state, o) if node.type == "stay" else None
        if risk and risk.score:
            total -= 0.12 * risk.score  # a likely disappointment costs a little: it never overrides a hard constraint
            crit[o.id]["risk"] = round(risk.score, 3)
        soft = [v for k, v in cons.items() if k not in hard_kinds and k != "free_text"]
        if soft:  # a soft wish counts a little: met adds, missed subtracts, unknown does nothing (it never decides alone)
            total += 0.04 * (soft.count("pass") - soft.count("fail"))
            crit[o.id]["wishes"] = round(soft.count("pass") / len(soft), 3)
        hint = history_hint(state, o)
        if hint:  # a nudge from the user's past trips, never a constraint
            total += 0.08 if hint == "like" else -0.15
            crit[o.id]["history"] = 1.0 if hint == "like" else 0.0
        scores[o.id] = OptionScore(offer_id=o.id, score=round(total, 4),
                                   criteria={k: round(v, 3) for k, v in crit[o.id].items()},
                                   constraints=cons, passes_hard="fail" not in hard_vals)

    def tier(s: OptionScore) -> int:
        hard_kinds = {c.kind for c in state.hard()}
        vals = [v for k, v in s.constraints.items() if k in hard_kinds]
        return 2 if "fail" in vals else 1 if "unknown" in vals else 0

    ordered = sorted(scores.values(), key=lambda s: (tier(s), -s.score))
    for i, s in enumerate(ordered, 1):
        s.rank = i
    best = next((s for s in ordered if tier(s) < 2), None)
    return scores, best.offer_id if best else None


def candidates(state: TripState, node: Node) -> list[str]:
    """Offers the budget optimiser may choose from: best tier available, top by score + cheapest."""
    sc = node.scores
    ids = [i for i in node.option_ids if i in sc and i in state.offers and i not in node.excluded_offers]
    hard_kinds = {c.kind for c in state.hard()}

    def tier(i: str) -> int:
        vals = [v for k, v in sc[i].constraints.items() if k in hard_kinds]
        return 2 if "fail" in vals else 1 if "unknown" in vals else 0
    usable = [i for i in ids if tier(i) == 0] or [i for i in ids if tier(i) == 1]
    usable.sort(key=lambda i: -sc[i].score)
    pick = usable[:6]
    if usable:
        cheapest = min(usable, key=lambda i: state.offers[i].price.amount)
        if cheapest not in pick:
            pick.append(cheapest)
    return pick


# ------------------------------------------------------------------ trade-offs

def tradeoffs(state: TripState, node: Node, k: int = 2) -> list[dict]:
    """How the selected offer differs from its nearest alternatives, as facts + Russian text."""
    sel = node.selected_offer_ref
    if not sel or sel not in node.scores:
        return []
    others = [s for s in sorted(node.scores.values(), key=lambda s: s.rank)
              if s.offer_id != sel and s.offer_id in state.offers and s.passes_hard]
    a = state.offers[sel]
    out = []
    for s in others[:k]:
        b = state.offers[s.offer_id]
        deltas = []
        dp = a.price.amount - b.price.amount
        if abs(dp) >= 1:
            deltas.append({"criterion": "price", "delta": str(dp),
                           "text": f"{'дороже' if dp > 0 else 'дешевле'} на {fmt.rub(abs(dp), a.price.currency)}"})
        if a.hotel and b.hotel and a.hotel.rating is not None and b.hotel.rating is not None:
            dr = a.hotel.rating - b.hotel.rating
            if abs(dr) >= 0.2:
                deltas.append({"criterion": "rating", "delta": round(dr, 2),
                               "text": f"рейтинг {'выше' if dr > 0 else 'ниже'} на {fmt.num(abs(dr))}"})
        if a.rate and b.rate:
            if a.rate.free_cancellation and not b.rate.free_cancellation:
                deltas.append({"criterion": "cancellation", "text": "есть бесплатная отмена, у альтернативы нет"})
            elif b.rate.free_cancellation and not a.rate.free_cancellation:
                deltas.append({"criterion": "cancellation", "text": "бесплатной отмены нет, у альтернативы есть"})
            if a.rate.breakfast_included and b.rate.breakfast_included is False:
                deltas.append({"criterion": "meal", "text": "завтрак включён, у альтернативы нет"})
            elif b.rate.breakfast_included and a.rate.breakfast_included is False:
                deltas.append({"criterion": "meal", "text": "без завтрака, у альтернативы он есть"})
        if a.transport and b.transport:
            dd = a.transport.duration_min - b.transport.duration_min
            if abs(dd) >= 20:
                deltas.append({"criterion": "duration", "delta": dd,
                               "text": f"в пути {'дольше' if dd > 0 else 'быстрее'} на {fmt.duration(abs(dd))}"})
            if a.transport.mode != b.transport.mode:
                deltas.append({"criterion": "mode", "text": f"вместо {_MODE_GEN.get(b.transport.mode, b.transport.mode)} — {_mode(a.transport.mode)}"})
        if deltas:
            out.append({"vs_offer_id": b.id, "vs_title": _vs_title(a, b), "deltas": deltas,
                        "text": "; ".join(d["text"] for d in deltas)})
    return out


def _vs_title(a: Offer, b: Offer) -> str:
    """Name of the alternative; another room of the same hotel is named by the room, not by the hotel again."""
    if a.hotel and b.hotel:
        if a.hotel.name == b.hotel.name and b.rate:
            return f"{b.rate.room_name}, этот же отель"
        return b.hotel.name
    return b.title()


_MODE_GEN = {"rail": "поезда", "flight": "самолёта", "bus": "автобуса", "drive": "авто"}


def _mode(m: str) -> str:
    return {"rail": "поезд", "flight": "самолёт", "bus": "автобус", "drive": "авто"}.get(m, m)


def why_selected(state: TripState, node: Node) -> list[str]:
    """Facts that make the selected offer fit THIS user, strongest user priorities first."""
    sel = node.selected_offer_ref
    if not sel or sel not in state.offers:
        return []
    o, sc = state.offers[sel], node.scores.get(sel)
    out: list[str] = []
    if history_hint(state, o) == "like":  # the most personal reason goes first (the list is capped)
        out.append("Вам понравился этот отель в прошлой поездке")
    out.extend(hotel_fit.aspect_why(state, o))
    if o.rate:
        if o.rate.breakfast_included:
            out.append("Завтрак включён")
        if o.rate.free_cancellation:
            until = o.rate.free_cancellation_until
            out.append("Бесплатная отмена" + (f" до {fmt.day(until)} {until:%H:%M}" if until else ""))
    if o.hotel:
        if o.hotel.rating is not None:
            out.append(f"Рейтинг {fmt.num(o.hotel.rating, 2)}/10" +
                       (f" по {o.hotel.review_count} отзывам" if o.hotel.review_count else ""))
    if o.transport:
        t = o.transport
        out.append(f"{_mode(t.mode).capitalize()}, в пути {fmt.duration(t.duration_min)}"
                   + (", без пересадок" if t.transfers == 0 else f", пересадок: {t.transfers}"))
    out.extend(hotel_fit.feedback_why(o))
    if sc and sc.rank == 1 and len(node.scores) > 1:
        out.append(f"Лучший по сумме критериев среди {len(node.scores)} вариантов")
    return out[:6]
