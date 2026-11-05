"""Destination agent: for «куда-нибудь к тёплому морю в ноябре» returns three explained options.

Code filters the curated catalog (tags, travel time, warm-sea threshold) and fetches real climate
for the requested month from Open-Meteo; the LLM only ranks the survivors and phrases the reasons
from those facts. If the LLM fails, the reasons come from the same facts deterministically.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date
from decimal import Decimal

import httpx

from ..config import Settings
from ..data.cities import CITIES
from ..data.destinations import DESTINATIONS
from ..domain import fmt
from ..domain.models import DestinationCandidate, Money, TripState
from ..integrations.hotels import HotelProvider, HotelQuery
from ..integrations.llm import LLM
from ..integrations.weather import Climate, month_climate
from .context import prompt
from .schemas import DestinationsOut

log = logging.getLogger("destinations")

# Russian word forms ("морю", "тёплом", "на лыжах") are matched by stems at a word start, after ё -> е.
KEYWORDS = {r"мор(?:е|ю|я|ем|ей|ск\w*)\b": "море", r"пляж": "море", r"океан": "море", r"купа(?:ть|ться|нь)": "море",
            r"гор(?:ы|ах|ам|ами|ны\w*|ный|ная|ное)\b": "горы", r"лыж": "лыжи", r"сноуборд": "лыжи", r"город": "город", r"культур": "культура",
            r"музе": "культура", r"гастро": "гастро", r"\bеда\b|\bеды\b|\bеде\b": "гастро", r"природ": "природа", r"озер": "озеро",
            r"дайвинг|\bныря": "дайвинг", r"тропик": "тропики", r"санатор": "санатории", r"шопинг": "шопинг"}
WARM = re.compile(r"\b(?:тепл|жарк|жар\b|жару|жары)")


def norm(text: str | None) -> str:
    return (text or "").lower().replace("ё", "е")


def wanted_tags(text: str | None) -> set[str]:
    t = norm(text)
    return {tag for rx, tag in KEYWORDS.items() if re.search(rx, t)}


def wants_warm(text: str | None) -> bool:
    return bool(WARM.search(norm(text)))


def wish_text(state: TripState) -> str:
    """The destination wish plus free-text conditions: all of it says what the traveller is after."""
    return " ".join([state.destination_text or ""] + [str(c.value) for c in state.constraints if c.kind == "free_text"])


def _month_of(state: TripState, today: date) -> tuple[int, int] | None:
    if state.date_from:
        return state.date_from.year, state.date_from.month
    if state.month_hint:
        y, m = state.month_hint.split("-")[:2]
        return int(y), int(m)
    return None


class DestinationAgent:
    def __init__(self, llm: LLM, hotels: HotelProvider, settings: Settings):
        self.llm, self.hotels, self.settings = llm, hotels, settings

    def shortlist(self, state: TripState) -> list[dict]:
        tags = wanted_tags(wish_text(state))
        max_h = next((float(c.value) for c in state.constraints if c.kind == "max_travel_hours"), None)
        origin_is_msk = state.origin in (None, "msk")

        def near_enough(d) -> bool:  # a travel-time limit is a hard condition: it is never lifted to get a longer list
            return not (max_h and origin_is_msk and d["hours_from_msk"] > max_h)
        near = [d for d in DESTINATIONS if near_enough(d)]
        if not tags:
            return near
        tagged = [d for d in near if tags & set(d["tags"])]
        if tagged or any(tags & set(d["tags"]) for d in DESTINATIONS):
            return tagged  # possibly empty: nothing with this tag within the limits, and we say so instead of widening them
        return near  # the catalog has no such tag at all (an unknown wish): do not filter on it

    async def suggest(self, state: TripState, today: date) -> tuple[list[DestinationCandidate], list[str]]:
        notes: list[str] = []
        ym = _month_of(state, today)
        wish = wish_text(state)
        want_sea = "море" in wanted_tags(wish)
        warm = wants_warm(wish)
        short = self.shortlist(state)[:14]
        climate: dict[str, Climate | None] = {}
        if ym:
            year = min(ym[0], today.year) - 1 if ym[0] >= today.year else ym[0]
            async with httpx.AsyncClient(timeout=15) as client:
                got = await asyncio.gather(*(month_climate(client, CITIES[d["city"]]["lat"], CITIES[d["city"]]["lon"],
                                                           d["sea"], year, ym[1]) for d in short))
            climate = {d["city"]: c for d, c in zip(short, got)}
            if not any(climate.values()):
                notes.append("Климат не удалось получить (Open-Meteo недоступен): подбор без погоды.")
        scored = []
        for d in short:
            c = climate.get(d["city"])
            s = 0.0
            if want_sea and d["sea"] and c and c.sea_c is not None:
                if warm and c.sea_c < self.settings.warm_sea_min_c:
                    continue  # the sea is not warm enough for «тёплое море»
                s += c.sea_c / 10
            elif want_sea and not d["sea"]:
                continue
            if c and c.air_c is not None and warm:
                s += c.air_c / 20
            if any(k.kind == "kid_friendly" for k in state.constraints) or state.party.children_ages or state.party.children_unknown:
                s += 0.5 if "дети" in d["tags"] else 0
            s -= d["hours_from_msk"] / 20
            scored.append((s, d, c))
        scored.sort(key=lambda t: -t[0])
        pool = scored[:6]
        if not pool:
            return [], notes + ["Под эти пожелания (с учётом ограничений по времени в пути) в каталоге ничего не нашлось."]

        def facts_of(d, c):
            city = CITIES[d["city"]]
            f = {"страна": city["country"], "теги": d["tags"], "перелёт_из_Москвы_ч (справочно)": d["hours_from_msk"],
                 "виза (справочно, уточняйте)": d["visa"]}
            if c:
                f["средняя_температура_воздуха_C"] = c.air_c
                if c.sea_c is not None:
                    f["температура_моря_C"] = c.sea_c
                f["осадки_мм_в_день"] = c.rain_mm_per_day
                f["климат_по_данным_за_год"] = c.year
            return f

        cand_facts = {d["city"]: {"name": CITIES[d["city"]]["name"], **facts_of(d, c)} for _, d, c in pool}
        picks_keys: list[tuple[str, list[str]]] = []
        try:
            user = json.dumps({"пожелание": state.destination_text, "месяц": ym, "дети": state.party.children_ages or state.party.children_unknown,
                               "бюджет": state.budget.qualitative or (str(state.budget.amount) if state.budget.amount else None),
                               "кандидаты": cand_facts}, ensure_ascii=False, default=str)
            out = await self.llm.structured(system=prompt("destinations"), user=user, schema=DestinationsOut)
            picks_keys = [(p.key, p.why) for p in out.picks if p.key in cand_facts][:3]
        except Exception as e:  # noqa: BLE001
            log.warning("destination LLM failed, using deterministic reasons: %s", e)
        if len(picks_keys) < 3:
            have = {k for k, _ in picks_keys}
            for _, d, c in pool:
                if len(picks_keys) >= 3:
                    break
                if d["city"] not in have:
                    picks_keys.append((d["city"], self._why(d, c)))
        by_city = {d["city"]: (d, c) for _, d, c in pool}
        cands = []
        for key, why in picks_keys[:3]:
            d, c = by_city[key]
            cand = DestinationCandidate(key=key, name=CITIES[key]["name"], country=CITIES[key]["country"], why=why[:3],
                                        facts=facts_of(d, c))
            await self._price_hint(cand, state, ym, today)
            cands.append(cand)
        return cands, notes

    @staticmethod
    def _why(d: dict, c: Climate | None) -> list[str]:
        out = []
        if c and c.sea_c is not None:
            out.append(f"Море {fmt.num(c.sea_c)} °C, воздух {fmt.num(c.air_c or 0)} °C")
        elif c and c.air_c is not None:
            out.append(f"Воздух около {fmt.num(c.air_c)} °C")
        out.append(f"Перелёт из Москвы около {fmt.num(d['hours_from_msk'])} ч (справочно)")
        out.append(f"Виза: {d['visa']} (справочно)")
        return out

    async def _price_hint(self, cand: DestinationCandidate, state: TripState, ym, today: date) -> None:
        """Rough hotel price level from the hotel source: cheapest offer per night for a short stay."""
        start = state.date_from or (date(ym[0], ym[1], 15) if ym else None)
        if not start or start <= today:
            return
        end = state.date_to if (state.date_from and state.date_to) else date.fromordinal(start.toordinal() + 3)
        try:
            res = await self.hotels.search(HotelQuery(city=cand.name, check_in=start, check_out=end,
                                                      adults=state.party.adults or 2, limit_hotels=3))
        except Exception as e:  # noqa: BLE001
            log.info("price hint failed for %s: %s", cand.name, e)
            return
        if not res.offers:
            return
        nights = max((end - start).days, 1)
        cheapest = min(res.offers, key=lambda o: o.price.amount)
        cand.price_hint = Money(amount=(cheapest.price.amount / nights).quantize(Decimal("1")), currency=cheapest.price.currency,
                                basis="per_night")
        cand.price_hint_mode = cheapest.data_mode
        cand.facts["ориентир_цены"] = f"отели от {fmt.rub(cand.price_hint.amount)} за ночь"
