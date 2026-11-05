"""Orchestrator: one user message (or UI action) -> new plan revision(s) + the main agent's answer.

Pipeline (see docs/PLAN.md §2):
  understand (planner || constraints || budget, one parallel LLM wave) -> apply typed ops
  -> ask ONE batch of blocking questions | suggest destinations | search -> rank -> pick set within budget
  -> transfers -> verify (+ repair) -> answer (LLM phrasing of code-built facts, number-checked).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal
from typing import Any, Callable, Protocol

from ..config import Settings
from ..domain import fmt, guard
from ..domain.apply import ApplyResult, apply_ops
from ..domain.budget import choose_set, compute_pricing
from ..domain.checks import run_checks
from ..domain.gaps import Gap, blocking_gaps, questions_text
from ..domain.graph import align_arrival_dates
from ..domain.models import Cart, DataNote, Question, Source, TripState
from ..domain.ops import (SetGuestFeedback, Ask, ChooseDestination, Op, ResolveQuestion, SelectOffer, SetChecks,
                          AcceptChecks, SetCart, SetDataNotes, SetDestinationCandidates, SetDates, SetRanking, SetStatus, SetTitle, UnpinOffer)
from ..domain.scoring import rank_node
from ..domain.turn import (COND_LABEL, AgentTurn, cond_label, contradicted_claims, risk_text, build_facts, unsupported_claims, changes_between, deterministic_text, recommendations,
                           node_label, understood, unsupported_numbers)
from ..integrations.llm import LLM, LLMError
from .context import all_prompts, calendar_anchors, prompt, user_prompt
from .deltas import budget_ops, constraints_ops, inherited_ops, planner_ops
from .destinations import DestinationAgent
from . import aftersales
from .schemas import Ambiguity, AnswerOut, BudgetOut, ConstraintIn, ConstraintsOut, PlannerOut
from .search import NodeSearchFailed, Searcher, plan_transfers

log = logging.getLogger("orchestrator")
REPAIRABLE = {"DATES_STAY_ARRIVAL", "DATES_STAY_DEPARTURE", "CONNECTION_TIME", "PLACE_CHAIN", "CONSTRAINT_FAIL"}


class PlanStore(Protocol):
    async def load(self, chat_id: str) -> TripState: ...
    async def save(self, chat_id: str, run_id: str, state: TripState, records: list[dict], changed: list[str],
                   label: str) -> TripState: ...
    async def emit(self, chat_id: str, run_id: str, type: str, payload: dict) -> None: ...
    async def inherited_items(self, chat_id: str) -> list[tuple[dict, str, str]]: ...
    async def cancel_order(self, chat_id: str) -> None: ...
    async def get_checkpoint(self, run_id: str) -> dict: ...
    async def set_checkpoint(self, run_id: str, data: dict) -> None: ...


@dataclass
class TurnResult:
    turn: AgentTurn
    state: TripState
    waiting_user: bool = False


@dataclass
class Run:
    chat_id: str
    run_id: str
    state: TripState
    before: TripState
    store: PlanStore
    records: list[dict] = field(default_factory=list)
    changed: set[str] = field(default_factory=set)
    failures: list[str] = field(default_factory=list)  # technical, for logs and the answer model
    missed: list[str] = field(default_factory=list)  # understanding agents that did not answer
    user_text: str = ""
    planner: PlannerOut | None = None
    cheaper: bool = False  # the user asked for a cheaper / the cheapest trip in this message
    no_cheaper: str | None = None  # said in the answer when nothing cheaper was found

    def apply(self, actor: str, ops: list[Op]) -> ApplyResult:
        res = apply_ops(self.state, actor, ops)
        self.state = res.state
        self.records += [{**r, "actor": actor} for r in res.applied + res.rejected]
        self.changed |= set(res.changed_node_ids)
        for r in res.rejected:
            log.info("op rejected (%s): %s", actor, r["reason"])
        return res

    async def checkpoint(self, label: str) -> None:
        self.state = await self.store.save(self.chat_id, self.run_id, self.state, self.records,
                                           sorted(self.changed), label)
        self.records, self.changed = [], set()

    async def progress(self, text: str, stage: str = "") -> None:
        await self.store.emit(self.chat_id, self.run_id, "agent.progress", {"text": text, "stage": stage})


def _has_selection(s: TripState) -> bool:
    return any(n.selected_offer_ref for n in s.nodes.values())


def field_satisfied(s: TripState, f: str) -> bool:
    return {
        "destination": bool(s.route) or s.destination_unresolved,
        "origin": s.origin is not None,
        "dates": bool(s.date_from and (s.date_to or all(x.nights for x in s.route))) if s.route else bool(s.date_from or s.month_hint),
        "party": s.party.adults is not None,
        "children_ages": not s.party.children_unknown,
    }.get(f, False)


HELP_OPTIONS = {
    "origin": ["Москва", "Санкт-Петербург", "Только отель, без билетов"],
    "dates": ["На ближайшие выходные", "Через неделю на 3 дня"],
    "party": ["Один", "Двое взрослых", "Двое взрослых и ребёнок"],
}
CHECKOUT_RE = re.compile(r"^(да|ок|окей|хорошо|согласен|согласна)?[,!. ]*(я\s+)?(хочу\s+|готов\s+|давайте\s+)?"
                        r"(оформ(ить|ляем|им|ляй|ляю)|куп(ить|лю)|покупаем|оплат(ить|им|ю)|оплачиваем|бронируем|беру|берём|как\s+оформить)"
                        r"( (поездку|заказ|всё|это|план|его))?[.!? ]*$"
                        r"|(я\s+)?согласен\s+(с\s+планом|на\s+план)|как\s+(мне\s+)?(оформить|купить|оплатить)|хочу\s+купить", re.I)
ACCEPT_DATES_RE = re.compile(r"принимаю\s+(новые\s+)?даты|согласен\s+с\s+(новыми\s+)?датами|даты\s+(устраивают|подходят)|оставь\s+как\s+есть", re.I)
_MONTHS = r"январ|феврал|март|апрел|мая|июн|июл|август|сентябр|октябр|ноябр|декабр"
_MONEY = re.compile(r"(?:до|за|бюджет\w*|не\s+дороже|максимум)\s*\d[\d\s.,]*\s*(?:к\b|тыс\w*|₽|руб\w*|р\b)?|\d[\d\s.,]*\s*(?:к\b|тыс\w*|₽|руб\w*|р\b)", re.I)
_DATE_SHAPES = re.compile(
    r"\d{1,2}\s*[.\-–—/]\s*\d{1,2}\b"                    # 14.11, 14-16
    rf"|\d{{1,2}}\s*(?:{_MONTHS})"                        # 14 ноября
    r"|\bс\s*\d{1,2}\b|\bпо\s*\d{1,2}\b|\d{1,2}\s*числ"   # с 14 по 16, 14 числа
    r"|\bна\s+\d+\s*(?:дн|ноч|недел|сут)|\b\d+\s*(?:дн|ноч)\w*"
    rf"|{_MONTHS}"
    r"|позже|раньше|завтра|послезавтра|сегодня|выходн|недел|месяц|дн[яей]\b|ноч|сдвин|перенес|перенос"
    r"|\b20\d\d\b", re.I)


def _mentions_dates(text: str) -> bool:
    """A date word or a date-shaped number in the message. Amounts of money ("до 5000", "30к") are not dates."""
    return bool(_DATE_SHAPES.search(_MONEY.sub(" ", text)))


def _drop_unmentioned_dates(planner: PlannerOut | None, text: str, trip_has_dates: bool = True) -> PlannerOut | None:
    """The model sometimes repeats a date change from the conversation history for a message that says nothing about
    dates ("Оформить" shifted the trip, "до 5000 за всё" restored old dates). Dates need a date word in THIS message."""
    if planner is None or not trip_has_dates or _mentions_dates(text):
        return planner
    if not (planner.shift_days or planner.date_from or planner.date_to or planner.nights_total or planner.month_hint):
        return planner
    log.warning("planner returned dates for a message without date words: dropped (%r)", text[:60])
    return planner.model_copy(update={"shift_days": None, "date_from": None, "date_to": None, "nights_total": None, "month_hint": None})


_MONEY_WORDS = re.compile(r"\d|тыс|₽|руб|бюджет|дешев|дорог|недорог|эконом|потрат|стоит|цен", re.I)


def _drop_unmentioned_budget(budget: BudgetOut | None, text: str) -> BudgetOut | None:
    """The budget agent sees the history and repeats an old amount for "Окей, хочу купить" (a "per person" amount was then
    multiplied a second time). An amount needs a money word or number in THIS message."""
    if budget is None or _MONEY_WORDS.search(text) or not (budget.amount is not None or budget.per_person):
        return budget
    log.warning("budget agent returned an amount for a message without money words: dropped (%r)", text[:60])
    return budget.model_copy(update={"amount": None, "per_person": False, "scope": None})


_NARROW_SCOPE = re.compile(r"только\s+(?:отел|жиль|проживан|билет|переезд|поезд|самол)|без\s+(?:билет|перелёт|перелет|переезд|поезд|самол)"
                           r"|билет\w*\s+не\s+нужн|не\s+нужн\w*\s+(?:билет|перелёт|перелет|переезд)|только\s+на\s+(?:поезд|самол)", re.I)


# internal ids and condition kinds that must never reach the user ("tutu_tr_…", "only_modes", "free_cancellation")
_INTERNAL_IDS = re.compile(r"(?<!\w)(?:tutu|mock)_\w+|\b(?:only|exclude|max|min)_[a-z_]+\b|\b(?:free_cancellation|pets_allowed|kid_friendly|bed_type|avoid_overnight)\b")
_VAGUE = re.compile(r"лыж|горнолыж|сноуборд|в\s+горы|к\s+морю|на\s+море|пляж|тепл\w+\s+(?:мор|стран|мест)|курорт", re.I)


def _vague_destination(planner: PlannerOut | None, text: str, state: TripState) -> PlannerOut | None:
    """"Куда поехать кататься на лыжах" names a mood, not a place: the destination step must run even if the model missed it."""
    if planner is None or state.route or state.destination_unresolved or planner.stops or planner.destination_vague:
        return planner
    m = _VAGUE.search(text)
    return planner.model_copy(update={"destination_vague": text.strip()[:120], "intent": "new_trip"}) if m else planner


def _keep_scope(planner: PlannerOut | None, text: str, state: TripState) -> PlannerOut | None:
    """A change to a trip that already has a scope ("убери завтрак") does not switch it to another one (tickets only ->
    hotel only, full -> hotel only) unless the user says so. Widening to the whole trip is always allowed."""
    if planner is None or not planner.scope or not state.route or state.scope in (None, planner.scope):
        return planner
    if planner.scope == "full" or _NARROW_SCOPE.search(text):
        return planner
    log.warning("planner switched the scope %s -> %s without a request: kept", state.scope, planner.scope)
    return planner.model_copy(update={"scope": None})


_NUM_WORDS = {"двое": 2, "двух": 2, "вдвоём": 2, "вдвоем": 2, "трое": 3, "троих": 3, "втроём": 3, "втроем": 3, "четверо": 4, "четверых": 4,
              "вчетвером": 4, "пятеро": 5, "впятером": 5, "шестеро": 6, "вшестером": 6}
_TOTAL_PARTY = re.compile(r"\bнас\s+(?:будет\s+|стало\s+|едет\s+|теперь\s+)?(\d{1,2}|двое|трое|четверо|пятеро|шестеро)\b|\b(\d{1,2})\s+(?:человек|чел\b)|\b(вдвоём|вдвоем|втроём|втроем|вчетвером|впятером|вшестером)\b", re.I)
_PEOPLE_WORDS = re.compile(r"\d|один|одна|одного|одну|сам\b|сама\b|\bя\b|\bмы\b|\bнас\b|вдво|втро|вчетв|впят|двое|трое|четв|пятер|взросл|человек|чел\b|"
                           r"реб[её]н|дет[еийя]|малыш|сын|доч|внук|жен[аоеуы]|муж|супруг|друг|друз|подруг|парн|девуш|семь|родител|мам|пап|бабуш|дед|коллег|компани|пара\b|парой",
                           re.I)

_UNSURE = re.compile(r"не\s+знаю|не\s+уверен|без\s+разницы|всё\s+равно|не\s+решил|затрудняюсь|пока\s+не", re.I)


_MODE_WORDS = {"rail": re.compile(r"поезд|\bжд\b|железнодорож", re.I), "bus": re.compile(r"автобус", re.I), "flight": re.compile(r"самол[её]т|авиа|лет(?:ать|еть|им)\b", re.I)}
_MODE_NEGATION = re.compile(r"\bбез\s+(?:самол|авиа|поезд|автобус)|\bне\s+(?:хочу\s+|надо\s+|нужн\w*\s+)?(?:на\s+)?(?:самол|авиа|поезд|автобус|лет)", re.I)
_MODE_OPEN = re.compile(r"\bили\b|любым|как\s+удобнее|на\s+выбор|не\s+важно|без\s+разницы", re.I)


def _fix_modes(cons: ConstraintsOut | None, text: str, state: TripState) -> ConstraintsOut | None:
    """"Только билеты на поезд" is a restriction to trains: the model took it for "tickets only" and searched flights.
    One transport named without "или" / a negation = only that mode; "без самолётов" = exclude it."""
    if cons is None or any(c.kind in ("only_modes", "exclude_modes") for c in (*cons.add, *state.constraints)) or _MODE_OPEN.search(text):
        return cons
    named = [m for m, rx in _MODE_WORDS.items() if rx.search(text)]
    if not named:
        return cons
    if _MODE_NEGATION.search(text):
        add = ConstraintIn(kind="exclude_modes", value=named, strength="hard", quote=text[:80])
    elif len(named) == 1:
        add = ConstraintIn(kind="only_modes", value=named, strength="hard", quote=text[:80])
    else:
        return cons
    log.warning("transport restriction missed by the model: %s %s (%r)", add.kind, named, text[:60])
    return cons.model_copy(update={"add": [*cons.add, add]})


def _total_party(text: str) -> int | None:
    m = _TOTAL_PARTY.search(text)
    if not m:
        return None
    w = next(g for g in m.groups() if g).lower()
    return int(w) if w.isdigit() else _NUM_WORDS.get(w)


def _fix_party(cons: ConstraintsOut | None, planner: PlannerOut | None, text: str, state: TripState) -> tuple[ConstraintsOut | None, PlannerOut | None]:
    """The headcount is arithmetic, not the model's job. "Нас будет четверо" is the TOTAL: the adults are what is left after the
    children (3 adults + 2 children, "четверо" used to become 4 adults + 2 children = 6 guests). A SHORT message with no number
    and no word about people never changes the party ("не знаю" once became "one adult")."""
    if cons is None:
        return cons, planner
    party_fields = ("adults", "add_adults", "children_ages", "add_children_ages", "children_unknown")
    if not _PEOPLE_WORDS.search(text):
        if len(text.split()) > 2 and not _UNSURE.search(text):
            # a full request ("отель в Москве") may carry a party the model inferred; the one thing it must not do is invent
            # "one adult" as a default (it did, for "Хочу в Питер на выходные", and the question "сколько вас" never came)
            if cons.adults == 1 and not cons.add_adults and state.party.adults is None:
                log.warning("constraints agent assumed one adult for %r: dropped, the party will be asked", text[:60])
                return cons.model_copy(update={"adults": None}), planner
            return cons, planner
        if any(getattr(cons, f) not in (None, [], 0) for f in party_fields):
            log.warning("constraints agent returned a party for a message about nobody: dropped (%r)", text[:60])
            cons = cons.model_copy(update={f: None for f in party_fields})
        return cons, planner
    total = _total_party(text)
    if total is None or total < 1:
        return cons, planner
    kids = (len(cons.children_ages) if cons.children_ages is not None else len(state.party.children_ages) + len(cons.add_children_ages or []))
    kids += cons.children_unknown if cons.children_unknown is not None else state.party.children_unknown
    if total - kids >= 1:
        return cons.model_copy(update={"adults": total - kids, "add_adults": None}), planner
    log.warning("headcount %d does not fit %d children: asking", total, kids)
    cons = cons.model_copy(update={"adults": None, "add_adults": None})
    if planner is not None:
        q = Ambiguity(field="party", text=f"Вас {total} вместе с детьми? Сколько среди вас взрослых?", options=["Двое взрослых", "Трое взрослых"])
        planner = planner.model_copy(update={"ambiguities": [*planner.ambiguities, q]})
    return cons, planner


def _fix_weekend(planner: PlannerOut | None, text: str) -> PlannerOut | None:
    """"На выходные" means Friday evening to Sunday (or Saturday to Sunday), whatever weekday the model landed on."""
    if planner is None or not planner.date_from or not re.search(r"выходн", text, re.I) or re.search(r"\d{1,2}\s*[.\-–—/]\s*\d{1,2}|\d{1,2}\s*(?:" + _MONTHS + ")", text, re.I):
        return planner
    d = planner.date_from
    if d.weekday() not in (4, 5):
        d = d + timedelta(days=(4 - d.weekday()) % 7)
    end = d + timedelta(days=(6 - d.weekday()) % 7)
    if (d, end) == (planner.date_from, planner.date_to):
        return planner
    log.warning("weekend dates corrected: %s..%s -> %s..%s", planner.date_from, planner.date_to, d, end)
    return planner.model_copy(update={"date_from": d, "date_to": end})


def _fix_past_dates(planner: PlannerOut | None, text: str, today) -> PlannerOut | None:
    """Dates the model put in the past: "20 ноября" said in December means next year; an explicit past year is a question."""
    if planner is None or not planner.date_from or planner.date_from >= today:
        return planner
    if not re.search(r"(?<!\d)20\d\d(?!\d)", text):
        try:
            moved = {k: getattr(planner, k).replace(year=getattr(planner, k).year + 1) for k in ("date_from", "date_to") if getattr(planner, k)}
        except ValueError:  # 29 February
            moved = {}
        if moved.get("date_from", today) >= today:
            return planner.model_copy(update=moved)
    return planner.model_copy(update={"date_from": None, "date_to": None, "ambiguities": [
        *planner.ambiguities, Ambiguity(field="dates", text="Эти даты уже прошли. На какие даты планируем поездку?")]})


class Orchestrator:
    def __init__(self, llm: LLM, searcher: Searcher, destinations: DestinationAgent, store: PlanStore,
                 settings: Settings, now: Callable[[], datetime] | None = None):
        self.llm, self.searcher, self.destinations, self.store, self.settings = llm, searcher, destinations, store, settings
        # values that must never appear in an answer to a user
        self.secrets = [v for v in (getattr(settings, "gigachat_auth_key", ""),) if v]
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.tz = ZoneInfo(getattr(settings, 'user_tz', None) or 'Europe/Moscow')

    def today(self) -> date:
        """Today for the user (Moscow by default), not for the server: at 00:49 MSK the server's UTC date is still yesterday."""
        return self.now().astimezone(self.tz).date()

    # ============================================================ entry points
    async def handle_message(self, chat_id: str, run_id: str, message_id: str, text: str, history: list[dict]) -> TurnResult:
        state = await self.store.load(chat_id)
        run = Run(chat_id, run_id, state, state.model_copy(deep=True), self.store, user_text=text)
        if (attack := guard.is_attack(text)):
            # a clear prompt-injection attempt: fixed refusal, no model call, the plan is not touched
            log.warning("prompt-injection attempt (%s) in chat %s: refused without calling the model", attack, chat_id)
            return TurnResult(AgentTurn(plan_revision=state.revision, kind="info", text=guard.REFUSAL), state)
        if aftersales.is_booked(state):
            return await self._after_sales(run, message_id, text)
        if ACCEPT_DATES_RE.search(text) and state.status in ("ready", "partial", "in_cart"):
            if any(c.code == "TRIP_DATES_MISMATCH" and c.severity == "error" for c in state.checks):
                return await self.handle_action(chat_id, run_id, {"type": "accept_dates"})
            return self._checkout_hint(run)  # nothing to accept: say where to pay
        if CHECKOUT_RE.search(text.strip()) and state.status in ("ready", "partial", "in_cart"):
            return self._checkout_hint(run)
        if (await self.store.get_checkpoint(run_id)).get("understood"):
            log.info("run %s resumes after the understanding step", run_id)  # do not apply the message twice
        else:
            await run.progress("Разбираю запрос", "understand")
            await self._understand(run, message_id, text, history)
            await self.store.set_checkpoint(run_id, {"understood": True})
        return await self._advance(run, question=text if (run.planner and run.planner.intent == "question") else None)

    async def handle_action(self, chat_id: str, run_id: str, action: dict[str, Any]) -> TurnResult:
        state = await self.store.load(chat_id)
        run = Run(chat_id, run_id, state, state.model_copy(deep=True), self.store)
        kind = action.get("type")
        if kind == "choose_destination":
            run.apply("user", [ChooseDestination(key=action["key"])])
            return await self._advance(run)
        if kind == "select_offer":
            run.apply("user", [SelectOffer(node_id=action["node_id"], offer_id=action["offer_id"], pin=action.get("pin", True))])
        elif kind == "unpin":
            run.apply("user", [UnpinOffer(node_id=action["node_id"])])
        elif kind == "accept_dates":
            keys = [c.facts["key"] for c in run.state.checks if c.code == "TRIP_DATES_MISMATCH" and c.facts.get("key")]
            run.apply("user", [AcceptChecks(keys=keys)] if keys else [])
        else:
            raise ValueError(f"unknown action '{kind}'")
        await self._after_selection(run)
        return await self._finalize(run, use_llm=False, kind="change")

    def _checkout_hint(self, run: Run) -> TurnResult:
        """"Оформить" is not a change of the plan: the order is placed in the web UI (cart, price re-check, payment form)."""
        s = run.state
        pricing = compute_pricing(s)
        total = fmt.rub(pricing["known_subtotal"]) if pricing["known_subtotal"] != "0.00" else None
        from shared.config import get_settings as shared_settings, refresh_public_url  # noqa: PLC0415
        refresh_public_url(shared_settings())
        base = shared_settings().public_base_url.rstrip("/")
        where = f"Откройте план по ссылке {base}/ и нажмите «Оформить»" if base.startswith("https://") else \
            "Откройте план в веб-интерфейсе (вкладка «План») и нажмите «Оформить»"
        blockers = [c for c in s.checks if c.severity == "error"]
        shift = next((c for c in blockers if c.code == "TRIP_DATES_MISMATCH"), None)
        if pricing["is_complete"] and shift and len(blockers) == 1:
            f = shift.facts
            text = (f"Оформить можно, когда вы примете сдвиг дат: проживание получилось на {fmt.day_iso(f['stays_from'])} — "
                    f"{fmt.day_iso(f['stays_to'])}, а вы просили {fmt.day_iso(f['requested_from'])} — {fmt.day_iso(f['requested_to'])} "
                    "(рейсы прилетают позже). Напишите «принимаю даты» или нажмите «Принять даты» в плане.")
            return TurnResult(AgentTurn(plan_revision=s.revision, kind="info", text=text,
                                        quick_replies=["Принимаю даты", "Другие даты"]), s)
        if not pricing["is_complete"]:
            why = []
            if pricing.get("needs_origin"):
                why.append("не указано, откуда вы едете, поэтому нет билетов туда и обратно")
            if pricing["missing"]:
                why.append("нет выбранного варианта для: " + ", ".join(node_label(s, s.nodes[m]) for m in pricing["missing"] if m in s.nodes))
            text = "План пока неполный (" + "; ".join(why or ["не все части подобраны"]) + "). Оформить можно, когда он будет собран: что поправить?"
        else:
            text = (f"План не меняю. {where}: там перепроверим цены и оформим заказ"
                    + (f" на {total}" if total else "") + ". Оплата имитируется: деньги не списываются.")
        return TurnResult(AgentTurn(plan_revision=s.revision, kind="info", text=text, quick_replies=["Дешевле", "На 2 дня позже"]), s)

    # ============================================================ after the purchase
    async def _after_sales(self, run: Run, message_id: str, text: str) -> TurnResult:
        s, today = run.state, self.today()
        pending = next((q for q in s.open_questions() if q.id.startswith("as_")), None)
        brief = {"order": s.cart.order_number, "dates": f"{s.date_from} — {s.date_to}", "pending": pending.text if pending else None,
                 "hotels": [o.hotel.name for _, o in aftersales._stays(s)]}
        user = json.dumps({"today": today.isoformat(), "calendar": calendar_anchors(today), "message": guard.sanitize_user_text(text),
                           "order": brief}, ensure_ascii=False)
        try:
            out = await self.llm.structured(system=prompt("aftersales"), user=user, schema=aftersales.AfterSalesOut)
        except LLMError as e:
            log.warning("aftersales agent failed: %s", e)
            out = aftersales.AfterSalesOut()
        await run.progress("Смотрю заказ", "aftersales")
        reply, replies = "", ["Ранний заезд", "Перенести даты", "Ключевые даты", "Отменить заказ"]
        order = aftersales.order_line(s).replace("Заказ", "заказ")
        if pending and out.intent == "confirm":
            return await self._confirm_pending(run, message_id, pending)
        if pending and out.intent == "decline":
            run.apply("planner", [ResolveQuestion(id=pending.id, answer="нет")])
            reply = f"Хорошо, {order} остаётся без изменений."
        elif out.intent == "early_checkin":
            reply = aftersales.hotel_request(s, "early")
        elif out.intent == "late_checkout":
            reply = aftersales.hotel_request(s, "late")
        elif out.intent == "transfer":
            reply = aftersales.transfer_info(s)
        elif out.intent == "reminders":
            reply = aftersales.reminders(s)
        elif out.intent == "new_trip":
            reply = (f"{aftersales.order_line(s)} уже оформлен, этот чат принадлежит ему. Для другой поездки начните новый чат, "
                     "прошлые вкусы подтянутся из истории.")
        elif out.intent == "cancel":
            note = aftersales.cancellation_note(s)
            self._ask_confirm(run, "as_cancel", "cancel", "Отменить заказ?")
            reply = (f"Отменить {order}? План сохранится, оформить заново можно в любой момент."
                     + (f"\n\nУсловия отмены:\n{note}" if note else "") + "\n\nПодтвердите: отменить?")
            replies = ["Да, отменить", "Нет, оставить"]
        elif out.intent == "change_dates":
            new_from, new_to = self._new_dates(s, out)
            if not (new_from and new_to) or new_from < today:
                reply = "На какие даты перенести? Назовите новые даты или на сколько дней сдвинуть."
            else:
                self._ask_confirm(run, "as_change", f"{new_from.isoformat()}|{new_to.isoformat()}", "Перенести даты?")
                note = aftersales.cancellation_note(s)
                reply = (f"Перенос на {fmt.day_range(new_from, new_to)}: оформленный заказ нужно отменить и собрать план заново, "
                         "цены и наличие могут измениться." + (f"\n\nУсловия отмены:\n{note}" if note else "")
                         + f"\n\nПодтвердите: отменить {order} и пересобрать план?")
                replies = ["Да, пересобрать", "Нет, оставить"]
        else:
            reply = (f"{aftersales.order_line(s)} оформлен (оплата имитирована). Что нужно: ранний заезд или поздний выезд, перенос дат, "
                     "трансфер, ключевые даты или отмена?")
        await run.checkpoint("Работа после покупки")
        turn = AgentTurn(plan_revision=run.state.revision, kind="info", text=reply, quick_replies=replies)
        return TurnResult(turn, run.state)

    @staticmethod
    def _new_dates(s: TripState, out) -> tuple:
        if not (s.date_from and s.date_to):
            return None, None
        if out.shift_days:
            d = timedelta(days=out.shift_days)
            return s.date_from + d, s.date_to + d
        if out.date_from:
            return out.date_from, out.date_to or out.date_from + (s.date_to - s.date_from)
        return None, None

    def _ask_confirm(self, run: Run, qid: str, payload: str, text: str) -> None:
        for q in run.state.open_questions():
            if q.id.startswith("as_"):
                run.apply("planner", [ResolveQuestion(id=q.id, answer="заменено")])
        run.apply("planner", [Ask(questions=[Question(id=qid, field="confirm", text=text, options=[payload])])])

    async def _confirm_pending(self, run: Run, message_id: str, pending) -> TurnResult:
        number = run.state.cart.order_number
        await self.store.cancel_order(run.chat_id)
        run.apply("planner", [ResolveQuestion(id=pending.id, answer="да")])
        run.apply("orchestrator", [SetCart(cart=Cart().model_dump(mode="json")),
                                   SetStatus(status="ready" if pending.id == "as_cancel" else "partial")])
        head = f"Заказ {number} отменён (имитация, деньги не списывались)."
        if pending.id == "as_cancel":
            await run.checkpoint("Заказ отменён")
            turn = AgentTurn(plan_revision=run.state.revision, kind="info", quick_replies=["Оформить"],
                             text=f"{head} План сохранён: можно оформить заново или изменить условия.")
            return TurnResult(turn, run.state)
        new_from, new_to = (date.fromisoformat(x) for x in pending.options[0].split("|"))
        run.apply("planner", [SetDates(date_from=new_from, date_to=new_to, source=Source(message_id=message_id, origin="user"))])
        await run.checkpoint("Заказ отменён, даты изменены")
        res = await self._advance(run)
        res.turn.text = f"{head} Собираю план на {fmt.day_range(new_from, new_to)}.\n\n{res.turn.text}"
        return res

    # ============================================================ understanding
    async def _understand(self, run: Run, message_id: str, text: str, history: list[dict]) -> None:
        s, today = run.state, self.today()
        user = user_prompt(text, s, history, today)

        async def call(name: str, schema):
            try:
                return await self.llm.structured(system=prompt(name), user=user, schema=schema)
            except LLMError as e:
                log.warning("%s agent failed: %s", name, e)
                run.failures.append(f"{name}: {e}")
                run.missed.append(name)
                return None
        planner, cons, budget = await asyncio.gather(call("planner", PlannerOut), call("constraints", ConstraintsOut),
                                                     call("budget", BudgetOut))
        if planner is None and cons is None and budget is None:
            raise LLMError("; ".join(run.failures) or "LLM did not answer")
        budget = _drop_unmentioned_budget(budget, text)
        planner = _vague_destination(planner, text, run.state)
        planner = _keep_scope(planner, text, run.state)
        planner = _drop_unmentioned_dates(planner, text, bool(run.state.date_from or run.state.month_hint))
        planner = _fix_weekend(planner, text)
        planner = _fix_past_dates(planner, text, today)
        cons, planner = _fix_party(cons, planner, text, run.state)
        cons = _fix_modes(cons, text, run.state)
        run.planner = planner
        pricing = compute_pricing(s)
        scoped = Decimal(pricing["budget_scoped_total"]) if pricing["known_subtotal"] != "0.00" else None
        if planner:
            run.apply("planner", planner_ops(planner, s, message_id))
        if cons:
            run.apply("constraints", constraints_ops(cons, run.state, message_id))
        if budget:
            run.cheaper = budget.direction in ("cheaper", "cheapest")
            run.apply("budget", budget_ops(budget, run.state, message_id, scoped))
        # answered questions: gaps by their field, model-raised ambiguities by the very next reply
        res = [ResolveQuestion(id=q.id, answer=run.user_text) for q in run.state.open_questions()
               if q.id.startswith("amb_") or field_satisfied(run.state, q.field)]
        if res:
            run.apply("planner", res)
        if planner:
            for a in planner.ambiguities:
                if not any(q.field == a.field and q.status == "open" for q in run.state.questions):
                    run.state.questions.append(Question(id=f"amb_{a.field}_{run.state.revision}", field=a.field,
                                                        text=a.text, options=a.options))
        # preferences the user accepted from past trips (history module): applied once per trip, never over what they said now
        if (ops := inherited_ops(run.state, await self.store.inherited_items(run.chat_id))):
            run.apply("constraints", ops)
        await run.checkpoint("Понял запрос")

    # ============================================================ pipeline
    async def _advance(self, run: Run, question: str | None = None) -> TurnResult:
        s = run.state
        gaps = blocking_gaps(s)
        extra = [Gap(q.field, q.text, q.options) for q in s.open_questions() if q.id.startswith("amb_")]
        if s.destination_unresolved and not gaps:
            return await self._destinations_turn(run)
        if extra or any(g.blocking for g in gaps):
            asked = {g.field for g in extra}  # a model-raised question about a field replaces the generic one
            return await self._ask_turn(run, [g for g in gaps if g.field not in asked] + extra)
        await self._search_and_pick(run)
        return await self._finalize(run, use_llm=True, kind="plan", question=question, follow_up=gaps)

    async def _ask_turn(self, run: Run, gaps: list[Gap]) -> TurnResult:
        previous = {q.field for q in run.state.open_questions()}
        if previous and previous == {g.field for g in gaps}:  # the same questions again: the answer did not move anything
            helped = True
        else:
            helped = False
        # answer buttons from the first ask on (a question without them forces the user to type "только отель" from memory)
        gaps = [Gap(g.field, g.text, HELP_OPTIONS.get(g.field, g.options), g.blocking) for g in gaps]
        rev = run.state.revision + 1
        run.apply("planner", [Ask(questions=[Question(id=f"q{rev}_{g.field}", field=g.field, text=g.text, options=g.options)
                                             for g in gaps])])
        run.apply("orchestrator", [SetStatus(status="needs_input")])
        await run.checkpoint("Нужны уточнения")
        qs = [{"id": q.id, "field": q.field, "text": q.text, "options": q.options} for q in run.state.open_questions()]
        text = questions_text(gaps)
        if helped:
            text = ("Пока не хватает ответа, чтобы продолжить. Выберите вариант кнопкой или напишите своими словами. "
                    "Если билеты не нужны, напишите «только отель».\n\n" + text)
        turn = AgentTurn(plan_revision=run.state.revision, kind="question", text=text,
                         understood=understood(run.state), questions=qs)
        return TurnResult(turn, run.state, waiting_user=True)

    async def _destinations_turn(self, run: Run) -> TurnResult:
        await run.progress("Подбираю направления", "destinations")
        cands, notes = await self.destinations.suggest(run.state, self.today())
        run.apply("destinations", [SetDestinationCandidates(candidates=cands)])
        run.apply("orchestrator", [SetStatus(status="choosing_destination"),
                                   SetDataNotes(scope_prefix="dest:", notes=[DataNote(scope="dest:climate", kind="partial", text=n) for n in notes])])
        await run.checkpoint("Предложены направления")
        if not cands:
            text = (notes[0] if notes else "Не нашёл подходящих направлений.") + " Опишите пожелания иначе или назовите место."
            return TurnResult(AgentTurn(plan_revision=run.state.revision, kind="info", text=text), run.state, waiting_user=True)
        lines = [f"Под «{run.state.destination_text}» подобрал три направления:"]
        for c in cands:
            lines.append(f"• {c.name} ({c.country})")
            lines += [f"  — {w}" for w in c.why]
            if c.facts.get("ориентир_цены"):
                lines.append(f"  — {c.facts['ориентир_цены']}" + (" (демо-данные)" if c.price_hint_mode == "mock" else ""))
        lines.append("Какое выбираем? Откуда вы едете и сколько вас, уточню после выбора. Цены в карточках ориентировочные, для двоих.")
        turn = AgentTurn(plan_revision=run.state.revision, kind="destinations", text="\n".join(lines),
                         understood=understood(run.state), quick_replies=[c.name for c in cands])
        await run.store.emit(run.chat_id, run.run_id, "destinations.ready", {"count": len(cands)})
        return TurnResult(turn, run.state, waiting_user=True)

    # ------------------------------------------------------------ search / rank / verify
    async def _search_and_pick(self, run: Run) -> None:
        s = run.state
        run.apply("orchestrator", [SetStatus(status="searching")])
        geo = await self.searcher.enrich_places(s)
        if geo:
            run.apply("orchestrator", [geo])
        await self._search_nodes(run)
        await run.progress("Сравниваю варианты и считаю бюджет", "rank")
        self._rank_and_choose(run)
        for _ in range(2):  # a late arrival moves check-in; the moved nodes are searched again for their new days
            moves = align_arrival_dates(run.state)
            if not moves:
                break
            run.apply("orchestrator", moves)
            first = next(m for m in moves if m.check_in)
            run.apply("orchestrator", [SetDataNotes(scope_prefix="dates_shift", notes=[DataNote(
                scope="dates_shift", kind="own_proposal",
                text=f"Из-за позднего прилёта заселение сдвинуто на {fmt.day(first.check_in)}; последующие даты сдвинуты так же, число ночей сохранено.")])])
            await run.progress("Двигаю даты под время прилёта", "dates")
            await self._search_nodes(run)
            self._rank_and_choose(run)
        await self._enrich_feedback(run)
        await self._after_selection(run, repair=True)
        for c in run.state.constraints:
            if c.kind == "free_text":
                run.apply("orchestrator", [SetDataNotes(scope_prefix="soft_text", notes=[DataNote(
                    scope="soft_text", kind="unverified", text=f"Пожелание «{c.label or c.value}» автоматически не проверяется: ориентируйтесь на описание вариантов.")])])
        self._not_pricier(run)
        await run.checkpoint("Выбраны варианты")

    def _not_pricier(self, run: Run) -> None:
        """"Сделай дешевле" must never end in a pricier trip: a visual run got +4 640 ₽ (a pricier hotel beat a cheaper flight).
        If the new set costs more than the old one, the old offers come back where they are still available; if nothing
        cheaper exists, the answer says so."""
        if not run.cheaper or not run.before.sequence:
            return
        old = compute_pricing(run.before)
        if not old["is_complete"]:
            return
        old_total = Decimal(old["budget_scoped_total"])
        if Decimal(compute_pricing(run.state)["budget_scoped_total"]) > old_total:
            ops: list[Op] = []
            for nid, n in run.state.nodes.items():
                b = run.before.nodes.get(nid)
                if b and b.selected_offer_ref and b.selected_offer_ref != n.selected_offer_ref and b.selected_offer_ref in n.option_ids \
                        and b.selected_offer_ref in n.scores:
                    ops.append(SetRanking(node_id=nid, scores=n.scores, selected=b.selected_offer_ref))
            if ops:
                run.apply("orchestrator", ops)
                log.warning("cheaper was asked but the new set cost more: previous offers restored for %d nodes", len(ops))
        new_total = Decimal(compute_pricing(run.state)["budget_scoped_total"])
        if new_total >= old_total:
            run.no_cheaper = (f"Дешевле варианта не нашлось: сумма осталась {fmt.rub(new_total, run.state.budget.currency)}"
                              if new_total == old_total else
                              f"Дешевле варианта подобрать не удалось: лучший из найденных стоит {fmt.rub(new_total, run.state.budget.currency)}, "
                              f"прежний стоил {fmt.rub(old_total, run.state.budget.currency)}")

    async def _search_nodes(self, run: Run) -> None:
        todo = [n for n in run.state.nodes.values() if n.type != "transfer" and n.status in ("empty", "stale")
                and ((n.type == "stay" and n.check_in and n.check_out) or (n.type == "transport" and n.departure_date))]
        if todo:
            await run.progress(f"Ищу варианты: {len(todo)} {'участок' if len(todo) == 1 else 'участка'}", "search")

        async def one(n):
            try:
                return n, await (self.searcher.search_stay(run.state, n) if n.type == "stay"
                                 else self.searcher.search_transport(run.state, n)), None
            except NodeSearchFailed as e:
                return n, None, e
        for n, ok, err in await asyncio.gather(*(one(n) for n in todo)):
            if err:
                run.failures.append(f"{n.id}: {err.reason}")
                run.apply("orchestrator", [SetDataNotes(scope_prefix=f"search:{n.id}", notes=[
                    DataNote(scope=f"search:{n.id}", kind="unverified",
                             text=f"{node_label(run.state, n)}: поиск не выполнен, {err.reason}.")])])
                continue
            attach, notes = ok
            run.apply("hotels" if n.type == "stay" else "transport", [attach])
            run.apply("orchestrator", [SetDataNotes(scope_prefix=f"search:{n.id}", notes=notes)])
        if todo:
            await run.checkpoint("Найдены варианты")

    async def _enrich_feedback(self, run: Run) -> None:
        """Quotes from guest reviews of the hotels that were chosen: one call per hotel, in parallel, never fatal."""
        todo = [(n, run.state.offers[n.selected_offer_ref]) for n in run.state.nodes.values()
                if n.type == "stay" and n.selected_offer_ref in run.state.offers]
        if not todo:
            return
        await run.progress("Читаю отзывы гостей", "reviews")
        ops = await asyncio.gather(*(self.searcher.hotel_feedback(run.state, n, o) for n, o in todo), return_exceptions=True)
        good = [op for op in ops if isinstance(op, SetGuestFeedback)]
        if good:
            run.apply("hotels", good)

    def _rank_and_choose(self, run: Run) -> None:
        ops: list[Op] = []
        for n in run.state.nodes.values():
            if n.type != "transfer" and n.option_ids:
                scores, best = rank_node(run.state, n)
                ops.append(SetRanking(node_id=n.id, scores=scores, selected=best if not n.pinned else n.selected_offer_ref))
        if ops:
            run.apply("orchestrator", ops)
        choice = choose_set(run.state)
        ops = [SetRanking(node_id=nid, scores=run.state.nodes[nid].scores, selected=oid)
               for nid, oid in choice.selected.items() if run.state.nodes[nid].option_ids and oid != run.state.nodes[nid].selected_offer_ref]
        if ops:
            run.apply("orchestrator", ops)

    def _apply_transfers(self, run: Run) -> None:
        places, transfers = plan_transfers(run.state)
        if places:
            run.apply("orchestrator", [places])
        run.apply("orchestrator", [transfers])

    async def _after_selection(self, run: Run, repair: bool = False) -> None:
        self._apply_transfers(run)
        checks = run_checks(run.state, self.now())
        if repair:
            for _ in range(2):
                errs = [c for c in checks if c.severity == "error" and c.code in REPAIRABLE]
                if not errs or not self._repair_once(run, errs):
                    break
                checks = run_checks(run.state, self.now())
        run.apply("verifier", [SetChecks(checks=checks)])

    def _repair_once(self, run: Run, errs) -> bool:
        """Try the next-best option on transport nodes involved in an error; keep it if errors drop."""
        def n_errors() -> int:
            self._apply_transfers(run)
            return sum(1 for c in run_checks(run.state, self.now()) if c.severity == "error")
        base = n_errors()
        involved = {nid for c in errs for nid in c.node_ids if run.state.nodes[nid].type == "transport" and not run.state.nodes[nid].pinned}
        for nid in sorted(involved):
            node = run.state.nodes[nid]
            ranked = [sc for sc in sorted(node.scores.values(), key=lambda x: x.rank)
                      if sc.passes_hard and sc.offer_id != node.selected_offer_ref]
            current = node.selected_offer_ref
            for sc in ranked:
                run.apply("orchestrator", [SelectOffer(node_id=nid, offer_id=sc.offer_id)])
                if n_errors() < base:
                    return True
            run.apply("orchestrator", [SelectOffer(node_id=nid, offer_id=current)]) if current else None
            n_errors()
        return False

    # ------------------------------------------------------------ answer
    async def _finalize(self, run: Run, use_llm: bool, kind: str, question: str | None = None,
                        follow_up: list[Gap] | None = None) -> TurnResult:
        repeated = False
        if follow_up:  # non-blocking questions ride along with the plan; the next reply resolves them
            prev = {q.field for q in run.before.open_questions()}
            if prev and prev == {g.field for g in follow_up}:  # the answer moved nothing: help instead of repeating
                repeated = True
            follow_up = [Gap(g.field, g.text, HELP_OPTIONS.get(g.field, g.options), g.blocking) for g in follow_up]
            rev = run.state.revision + 1
            run.apply("planner", [Ask(questions=[Question(id=f"q{rev}_{g.field}", field=g.field, text=g.text, options=g.options)
                                                 for g in follow_up])])
        s = run.state
        has_errors = any(c.severity == "error" for c in s.checks)
        pricing = compute_pricing(s)
        status = "ready" if (pricing["is_complete"] and not has_errors) else "partial"
        names = ", ".join(s.places[x.place].name or x.place for x in s.route if x.place in s.places)
        run.apply("orchestrator", [SetStatus(status=status)])
        run.apply("planner", [SetTitle(title=f"{names}, {fmt.day_range(s.date_from, s.date_to)}" if names else "Поездка")])
        await run.checkpoint("План собран")
        s = run.state
        facts = build_facts(s, run.before if run.before.sequence else None)
        if question:
            facts["question"] = guard.sanitize_user_text(question)
        if run.failures:
            facts["failures"] = run.failures
        turn_kind = "change" if kind == "change" or (facts["changes"] and _has_selection(run.before)) else kind
        base_text = deterministic_text(s, facts, turn_kind)
        text = base_text
        if use_llm and facts["recommendations"]:
            facts_text = json.dumps(facts, ensure_ascii=False, default=str)
            feedback = ""
            for attempt in (1, 2):  # a rejected answer gets ONE rewrite with the reason before the plain template
                try:
                    out = await self.llm.structured(system=prompt("answer"), user=facts_text + feedback,
                                                    schema=AnswerOut, max_tokens=1800)
                except LLMError as e:
                    log.warning("answer LLM failed, using template: %s", e)
                    break
                bad = unsupported_numbers(out.text, facts, extra=base_text)
                leaks = guard.answer_leaks(out.text, facts_text + base_text, self.secrets, all_prompts())
                leaks += unsupported_claims(out.text, facts_text + " " + base_text)
                leaks += contradicted_claims(out.text, s)
                if _INTERNAL_IDS.search(out.text):
                    leaks.append("внутренние идентификаторы")
                if 40 <= len(out.text) <= 3000 and not bad and not leaks:
                    text = out.text
                    break
                log.warning("LLM answer rejected, attempt %d (numbers not in facts: %s; unsafe: %s)", attempt, sorted(bad)[:6], leaks)
                feedback = ("\n\nПРОШЛЫЙ ОТВЕТ ОТКЛОНЁН, перепиши. "
                            + (f"Этих чисел нет в фактах: {', '.join(sorted(bad)[:6])}. Бери только числа из фактов в их точном виде, "
                               "ничего не складывай и не вычитай. " if bad else "")
                            + (f"Нельзя: {'; '.join(map(str, leaks[:3]))}. " if leaks else "")
                            + ("Длина ответа 40–3000 символов." if not 40 <= len(out.text) <= 3000 else ""))
            else:
                log.warning("LLM answer rejected twice, using template")
        # a condition the source could not confirm (or the pick breaks) must be in the text, whatever the model wrote
        unmet = [c for c in s.checks if c.code in ("CONSTRAINT_UNKNOWN", "CONSTRAINT_FAIL")]
        lacking = [risk_text(c) for c in unmet if COND_LABEL.get(c.facts.get("kind"), c.facts.get("kind", "")).lower()[:8] not in text.lower()]
        if lacking:
            text += "\n\nПроверьте вручную: " + "; ".join(dict.fromkeys(lacking)) + "."
        hard_kinds = {c.kind for c in s.hard()}
        confirmed = []
        for n in s.nodes.values():  # the user's special conditions that the source DID confirm for the chosen stay
            o, sc = s.offers.get(n.selected_offer_ref or ""), n.scores.get(n.selected_offer_ref or "")
            if n.type == "stay" and o and sc:
                confirmed += [COND_LABEL.get(k, k) for k, st in sc.constraints.items()
                              if st == "pass" and k in hard_kinds and k in ("pets_allowed", "kid_friendly", "bed_type", "view")]
        confirmed = [x for x in dict.fromkeys(confirmed) if x.lower()[:8] not in text.lower()]
        if confirmed:
            text += "\n\nПодтверждено источником для выбранного отеля: " + "; ".join(confirmed) + "."
        history = list(dict.fromkeys(
            (c.source.quote or "").strip() for c in (*s.constraints, *s.priorities)
            if c.source and c.source.origin == "history" and c.source.quote))
        if history and "прошл" not in text.lower():
            text += "\n\nУчтены вкусы из прошлых поездок: " + "; ".join(history[:3]) + "."
        text = fmt.fix_money(text)
        if status != "ready":  # nothing to buy yet: do not invite to check out
            text = re.sub(r"(?:Что дальше\?\s*|Следующий шаг:?\s*)?[Оо]формляем\?|Подтверждаем бронирование\?", "Что поправить?", text)
        if run.no_cheaper:
            text += "\n\n" + run.no_cheaper + "."
        failed = [risk_text(c) for c in s.checks if c.severity == "error" and c.code not in ("CONSTRAINT_UNKNOWN", "CONSTRAINT_FAIL")]
        failed = [t for t in dict.fromkeys(failed) if t.lower()[:18] not in text.lower()]
        if failed:  # a failed consistency check is never left out of the answer
            text += "\n\nНе согласовано: " + "; ".join(failed) + "."
        mock_kinds = {"flight": "перелёты", "rail": "поезда", "bus": "автобусы"}
        demo = sorted({mock_kinds.get(o.transport.mode, "переезды") for n in s.nodes.values() if n.type == "transport"
                       for o in [s.offers.get(n.selected_offer_ref or "")] if o and o.data_mode == "mock" and o.transport})
        if demo and "демо" not in text.lower():
            text += f"\n\nВажно: {', '.join(demo)} — демо-данные, цены и расписание условны."
        # failed searches are already explained by their data notes; raw errors stay in the log
        if run.missed:
            text += "\n\nЧасть запроса разобрать не удалось: проверьте, что я понял верно, и поправьте, если нет."
        if follow_up:
            if repeated:
                text += "\n\nПока не хватает ответа. Выберите вариант кнопкой или напишите своими словами; если билеты не нужны, напишите «только отель»."
            text += "\n\n" + questions_text(follow_up)
        replies = ["Дешевле", "На 2 дня позже", "Добавить взрослого"]
        if any(c.code == "TRIP_DATES_MISMATCH" and c.severity == "error" for c in s.checks):
            replies.insert(0, "Принимаю даты")
        if status == "ready":
            replies.append("Оформить")
        else:
            soft = [c for c in s.constraints if c.kind in ("breakfast", "free_cancellation") and c.strength == "hard"]
            replies += [f"Убрать условие «{cond_label(c)}»" for c in soft][:2]
        turn = AgentTurn(plan_revision=s.revision, kind=turn_kind, text=text, understood=understood(s),
                         recommendations=recommendations(s), changes=changes_between(run.before if run.before.sequence else None, s),
                         quick_replies=replies,
                         questions=[{"id": q.id, "field": q.field, "text": q.text, "options": q.options}
                                    for q in s.open_questions()] if follow_up else [])
        await run.store.emit(run.chat_id, run.run_id, "plan.ready", {"revision": s.revision, "status": s.status})
        return TurnResult(turn, s)
