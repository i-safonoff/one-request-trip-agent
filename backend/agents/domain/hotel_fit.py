"""Does this hotel suit THIS guest? The "hotel" side of the track's user story: recommend a hotel to the guests it really
fits, so there are fewer cancellations and disappointed reviews.

Three things, all computed from data the source returns (never invented, never by a model):
* `aspect_why`        guest ratings by aspect (cleanliness, location, ...) that matter for what the user asked;
* `disappointment`    a 0..1 risk with the reasons: few reviews, a weak aspect, a mismatch with the party, a rate that
                      cannot be cancelled. It also costs the offer a little score;
* `pick_feedback`     short quotes from reviews of similar guests that are relevant to this trip (a child, noise,
                      the check-in queue), after `guard.clean_snippet`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from . import fmt, guard
from .models import Offer, TripState

ASPECT_FOR_PRIORITY = {"price": "Цена/Качество", "location": "Расположение", "meal": "Питание", "stars": "Комфорт"}
ASPECT_FOR_CONSTRAINT = {"breakfast": "Питание"}
ALWAYS_MATTERS = ("Чистота",)
WEAK_ASPECT = 7.0
STRONG_ASPECT = 8.5

# what guests complain about that decides a stay; each topic is matched by word stems
CON_TOPICS = {
    "cleanliness": ("грязн", "чистот", "запах", "вонь", "таракан", "клоп", "плесен", "пятн"),
    "noise": ("шум", "гром", "гул", "слышн", "стройк", "орут"),
    "checkin": ("очеред", "заселен", "ресепшен", "регистрац", "ключ"),
    "comfort": ("жара", "душно", "кондиционер", "холодно", "кровать", "матрас", "скрип", "старая", "ремонт"),
    "equipment": ("wi-fi", "wifi", "вай-фай", "лифт", "не работа", "сломан"),
}
PARTY_TOPICS = {
    "child": ("ребен", "ребён", "дет", "малыш", "коляск", "семь"),
    "pet": ("собак", "кошк", "питомц", "животн"),
}
PRIORITY_TOPICS = {
    "price": ("цен", "стоим", "дорог", "дешев", "оправдан"),
    "location": ("расположен", "метро", "рядом", "далеко", "центр", "вокзал", "пешком"),
    "meal": ("завтрак", "питан", "еда", "кормят", "ужин"),
}
LINEUP_FOR_PARTY = {"family": ("family", "child", "kid", "дет", "семь"), "couple": ("couple", "близк"), "single": ("single", "только я"),
                    "group": ("group", "friends", "компани")}


@dataclass
class Risk:
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


def _n(x: float) -> str:
    return fmt.num(x, 2) if round(x, 1) != round(x, 2) else fmt.num(x, 1)


def _topics(state: TripState) -> tuple[set[str], set[str]]:
    """(aspects that matter to this user, party topics)"""
    aspects = set(ALWAYS_MATTERS)
    for p in state.priorities:
        if p.key in ASPECT_FOR_PRIORITY:
            aspects.add(ASPECT_FOR_PRIORITY[p.key])
    for c in state.constraints:
        if c.kind in ASPECT_FOR_CONSTRAINT:
            aspects.add(ASPECT_FOR_CONSTRAINT[c.kind])
    party: set[str] = set()
    if state.party.children_ages or state.party.children_unknown:
        party.add("child")
    if state.party.pets:
        party.add("pet")
    return aspects, party


def aspect_why(state: TripState, offer: Offer, limit: int = 2) -> list[str]:
    """Strong guest ratings for exactly what the user cares about: 'Расположение 9,2 из 10 по оценкам гостей'."""
    h = offer.hotel
    if not h or not h.aspects:
        return []
    wanted, _ = _topics(state)
    strong = sorted(((a, v) for a, v in h.aspects.items() if a in wanted and v >= STRONG_ASPECT and a not in ALWAYS_MATTERS or
                     (a in ALWAYS_MATTERS and v >= 9.0)), key=lambda t: -t[1])
    return [f"{a}: {_n(v)} из 10 по оценкам гостей" for a, v in strong[:limit]]


def disappointment(state: TripState, offer: Offer) -> Risk:
    """Risk that the guest ends up disappointed or cancels, with reasons built from the offer's own data."""
    r = Risk()
    h, rate = offer.hotel, offer.rate
    if not h:
        return r
    wanted, party = _topics(state)
    if h.review_count is not None:
        if h.review_count < 30:
            r.score += 0.25
            r.reasons.append(f"отзывов мало: {h.review_count}, оценка может быть неточной")
        elif h.review_count < 100:
            r.score += 0.08
    if h.rating is not None and h.rating < 7.5:
        r.score += 0.25
        r.reasons.append(f"общая оценка гостей невысокая: {_n(h.rating)} из 10")
    elif h.rating is not None and h.rating < 8.0:
        r.score += 0.08
    weak = sorted((a, v) for a, v in h.aspects.items() if v < WEAK_ASPECT)
    for a, v in weak[:3]:
        mine = a in wanted
        r.score += 0.30 if mine else 0.12
        r.reasons.append(f"слабое место по оценкам гостей: {a} {_n(v)} из 10" + (" (для вас это важно)" if mine else ""))
    if rate is not None and rate.free_cancellation is False:
        r.score += 0.10
        r.reasons.append("у тарифа нет бесплатной отмены: условия возврата уточняйте у отеля")
    if "child" in party and h.kid_friendly is None:
        r.score += 0.08
        r.reasons.append("для поездки с ребёнком детские удобства источник не подтверждает")
    if "pet" in party and h.pets_allowed is None:
        r.score += 0.08
        r.reasons.append("размещение с животными источник не подтверждает")
    r.score = min(r.score, 1.0)
    return r


# ---------------------------------------------------------------- guest reviews

@dataclass
class Review:
    rating: float | None
    created: datetime | None
    lineup: str  # "single" / "couple" / "group" / "family" text as the source gives it, lower case
    pros: str | None
    cons: str | None


def _stem_match(text: str, stems: tuple[str, ...]) -> bool:
    t = text.lower()
    return any(s in t for s in stems)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _best_sentence(text: str, stem_groups: list[tuple[str, ...]], prefer: list[tuple[str, ...]] | None = None) -> str | None:
    """First sentence about a topic; a sentence about the `prefer` topics (this party) wins over an earlier one."""
    sents = _sentences(text)
    for groups in ([prefer] if prefer else []) + [stem_groups]:
        for sent in sents:
            if any(_stem_match(sent, g) for g in groups) and (q := guard.clean_snippet(sent)):
                return q
    return None


def _party_lineup(state: TripState) -> str:
    if state.party.children_ages or state.party.children_unknown:
        return "family"
    return "single" if (state.party.adults or 0) == 1 else "group" if (state.party.adults or 0) >= 3 else "couple"


def pick_feedback(state: TripState, reviews: list[Review], limit: int = 2) -> tuple[list[str], list[str], int]:
    """Quotes that matter for this trip. Reviews of similar guests (a family for a family) come first, then the newest.

    Cons are taken only where a guest names something that decides a stay (cleanliness, noise, the check-in queue,
    comfort, equipment) or something about this party (a child, a pet); pros only for what the user asked about.
    """
    _, party = _topics(state)
    stems_pro = [PRIORITY_TOPICS[p.key] for p in state.priorities if p.key in PRIORITY_TOPICS]
    if any(c.kind == "breakfast" for c in state.constraints):
        stems_pro.append(PRIORITY_TOPICS["meal"])
    stems_party = [PARTY_TOPICS[k] for k in party]
    stems_con = list(CON_TOPICS.values()) + stems_party
    want = LINEUP_FOR_PARTY[_party_lineup(state)]

    def rank(rv: Review) -> tuple[int, int, float]:
        similar = any(w in rv.lineup for w in want)
        about_party = bool(rv.cons) and any(_stem_match(rv.cons, g) for g in stems_party)
        return (0 if similar else 1, 0 if about_party else 1, -(rv.created.timestamp() if rv.created else 0.0))
    pros: list[str] = []
    cons: list[str] = []
    for rv in sorted(reviews, key=rank):
        if rv.cons and len(cons) < limit and (q := _best_sentence(rv.cons, stems_con, stems_party)) and q not in cons:
            cons.append(q)
        if rv.pros and stems_pro and len(pros) < limit and (q := _best_sentence(rv.pros, stems_pro)) and q not in pros:
            pros.append(q)
    return pros, cons, len(reviews)


def feedback_why(offer: Offer) -> list[str]:
    h = offer.hotel
    return [f"Гости пишут: «{h.pros[0]}»"] if h and h.pros else []


def feedback_risks(offer: Offer) -> list[str]:
    h = offer.hotel
    return [f"Гости жалуются: «{c}»" for c in (h.cons[:2] if h else [])]
