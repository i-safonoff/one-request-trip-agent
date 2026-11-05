"""What the LLM agents return: small, flat *deltas* (not paths, not patches).

Code maps each delta onto typed operations (agents/deltas.py), so the model can never address
arbitrary parts of the state and a malformed field only loses that field.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class StopDelta(BaseModel):
    name: str = Field(description="Город/место, как назвал пользователь (в именительном падеже)")
    nights: int | None = Field(default=None, description="Число ночей, только если пользователь назвал")


class Ambiguity(BaseModel):
    field: str
    text: str = Field(description="Короткий вопрос пользователю")
    options: list[str] = []


class PlannerOut(BaseModel):
    intent: Literal["new_trip", "modify", "choose", "question", "other"] = "other"
    scope: Literal["stay", "transport", "full"] | None = Field(
        default=None, description="stay — только жильё; transport — только билеты; full — поездка целиком")
    origin: str | None = Field(default=None, description="Город отправления, если назван")
    stops: list[StopDelta] | None = Field(
        default=None, description="ПОЛНЫЙ упорядоченный список городов маршрута, если маршрут назван или менялся")
    round_trip: bool | None = None
    date_from: date | None = None
    date_to: date | None = None
    nights_total: int | None = Field(default=None, description="Общее число ночей, если назвали срок, а не даты")
    shift_days: int | None = Field(default=None, description="На сколько дней сдвинуть даты («на два дня позже» = 2)")
    month_hint: str | None = Field(default=None, description="Месяц в формате ГГГГ-ММ, если даты неточные")
    destination_vague: str | None = Field(
        default=None, description="Если конкретного места нет, а есть пожелание («тёплое море», «горы») — оно здесь")
    pin_nodes: list[str] = Field(default=[], description="id узлов плана, которые пользователь просит оставить")
    replace_nodes: list[str] = Field(default=[], description="id узлов плана, которые пользователь просит заменить")
    ambiguities: list[Ambiguity] = []
    quote: str | None = Field(default=None, description="Короткая цитата из сообщения, на которой основаны поля")


class ConstraintIn(BaseModel):
    kind: str
    value: Any = True
    strength: Literal["hard", "soft"] = "hard"
    label: str | None = None
    quote: str | None = None


class PriorityDelta(BaseModel):
    key: str
    weight: float = 1.0


class ConstraintsOut(BaseModel):
    adults: int | None = None
    add_adults: int | None = None
    children_ages: list[int] | None = None
    add_children_ages: list[int] | None = None
    children_unknown: int | None = Field(default=None, description="Сколько детей, если возраст не назван")
    pets: list[str] | None = None
    add: list[ConstraintIn] = []
    remove: list[str] = Field(default=[], description="kind условий, которые пользователь снимает")
    relax: list[str] = Field(default=[], description="kind условий, которые пользователь делает необязательными")
    priorities: list[PriorityDelta] = []
    replace_priorities: bool = False
    quote: str | None = None


class BudgetOut(BaseModel):
    amount: Decimal | None = Field(default=None, description="Сумма цифрой, если названа")
    scope: Literal["trip", "stay", "transport"] | None = None
    per_person: bool = Field(default=False, description="true, если сумма названа «на человека», «на каждого», «с человека»")
    qualitative: str | None = Field(default=None, description="«недорого», «подешевле», если суммы нет")
    direction: Literal["cheaper", "cheapest"] | None = Field(
        default=None, description="cheaper — «сделай дешевле»; cheapest — «самое дешёвое»")
    quote: str | None = None


class DestinationPick(BaseModel):
    key: str = Field(description="key из списка кандидатов")
    why: list[str] = Field(description="2-3 причины, только из переданных фактов")


class DestinationsOut(BaseModel):
    picks: list[DestinationPick]


class AnswerOut(BaseModel):
    text: str = Field(description="Ответ пользователю по-русски, только на основе переданных фактов")
