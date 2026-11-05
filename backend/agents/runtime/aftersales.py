"""After the purchase: early check-in, a change of dates, transfer, reminders, cancellation.

The order is simulated (nothing is booked with suppliers), so every answer here is built by code from the plan and
says plainly what is and is not done. The model only classifies the message (`AfterSalesOut`); it never writes the reply.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..domain import fmt
from ..domain.geo import to_local
from ..domain.models import Node, Offer, TripState

HONEST = "Бронирования имитируются: ничего не отправляется поставщикам, текст можно скопировать и отправить самому."


class AfterSalesOut(BaseModel):
    intent: Literal["early_checkin", "late_checkout", "change_dates", "transfer", "reminders", "cancel", "new_trip",
                    "confirm", "decline", "other"] = "other"
    shift_days: int | None = Field(default=None, description="На сколько дней сдвинуть поездку (минус — раньше)")
    date_from: date | None = None
    date_to: date | None = None


def is_booked(state: TripState) -> bool:
    return state.status == "booked" or (state.cart.status == "paid" and bool(state.cart.order_number))


def _stays(state: TripState) -> list[tuple[Node, Offer]]:
    out = []
    for nid in state.sequence:
        n = state.nodes[nid]
        o = state.offers.get(n.selected_offer_ref or "")
        if n.type == "stay" and o and o.hotel and n.check_in:
            out.append((n, o))
    return out


def _arrival_before(state: TripState, stay: Node) -> tuple[datetime | None, str]:
    """Local arrival time and a label ("рейс …", "поезд …") of the transport that brings the guest to this stay."""
    i = state.sequence.index(stay.id)
    for nid in reversed(state.sequence[:i]):
        n = state.nodes[nid]
        if n.type == "transport":
            o = state.offers.get(n.selected_offer_ref or "")
            if o and o.transport and o.transport.segments:
                seg = o.transport.segments[-1]
                place = state.places.get(stay.place or "")
                label = {"flight": "рейс", "rail": "поезд", "bus": "автобус"}.get(o.transport.mode, "переезд")
                return to_local(seg.arrival, place.tz if place else None), f"{label} {seg.number or ''}".strip()
        if n.type == "stay":
            break
    return None, ""


def _party(state: TripState) -> str:
    p = state.party
    kids = len(p.children_ages) + p.children_unknown
    return f"гостей: {p.adults or 1}" + (f", из них детей: {kids}" if kids else "")


def order_line(state: TripState) -> str:
    return f"Заказ {state.cart.order_number}" if state.cart.order_number else "Заказ"


def hotel_request(state: TripState, kind: Literal["early", "late"]) -> str:
    stays = _stays(state)
    if not stays:
        return f"{order_line(state)} оформлен, но в нём нет проживания: ранний заезд и поздний выезд относятся к отелю."
    lines = [f"{order_line(state)}: {'ранний заезд' if kind == 'early' else 'поздний выезд'}."]
    for n, o in stays:
        h = o.hotel
        arr, how = _arrival_before(state, n)
        std = (h.check_in_time if kind == "early" else h.check_out_time) or None
        period = f"{fmt.day(n.check_in)} — {fmt.day(n.check_out)}"
        std_text = (f"Стандартное время {'заселения' if kind == 'early' else 'выезда'}: {std}." if std
                    else "Стандартное время в данных не указано, уточните у отеля.")
        arrival = f" Вы приезжаете {fmt.day(arr)} в {arr:%H:%M} ({how})." if kind == "early" and arr else ""
        lines.append(f"\n• {h.name}, {period}. {std_text}{arrival}")
        if kind == "early":
            text = (f"Здравствуйте! Бронь {state.cart.order_number}, {h.name}, {_party(state)}, заезд {fmt.day(n.check_in)}. "
                    + (f"Мы прибываем в {arr:%H:%M}. " if arr else "")
                    + "Возможно ли заселение раньше стандартного времени? Если нет, можно ли оставить багаж?")
        else:
            text = (f"Здравствуйте! Бронь {state.cart.order_number}, {h.name}, {_party(state)}, выезд {fmt.day(n.check_out)}. "
                    "Возможен ли поздний выезд и на каких условиях?")
        lines.append(f"  Текст запроса в отель: «{text}»")
    lines.append("\nЭто опция отеля, доступность зависит от загрузки, может быть платной. " + HONEST)
    return "\n".join(lines)


def transfer_info(state: TripState) -> str:
    stays = _stays(state)
    if not stays:
        return f"{order_line(state)} оформлен без проживания: адрес для трансфера неизвестен."
    lines = [f"{order_line(state)}: трансфер."]
    for n, o in stays:
        h = o.hotel
        arr, how = _arrival_before(state, n)
        where = f"В отель {h.name}" + (f", {h.address}" if h.address else "")
        when = f". Вы прибываете {fmt.day(arr)} в {arr:%H:%M} ({how})." if arr else "."
        lines.append(f"\n• {where}{when}")
    lines.append("\nТрансфер у поставщика я не заказываю: такси и трансферы в этой версии не продаются. Сообщите адрес и "
                 "время прибытия выше в службу такси или водителю. " + HONEST)
    return "\n".join(lines)


def reminders(state: TripState) -> str:
    items: list[tuple[date, str]] = []
    for nid in state.sequence:
        n = state.nodes[nid]
        o = state.offers.get(n.selected_offer_ref or "")
        if n.type == "transport" and o and o.transport and o.transport.segments:
            dep = o.transport.segments[0].departure
            advice = {"flight": "в аэропорт приезжайте за 2–3 часа", "rail": "на вокзал приезжайте за 40 минут",
                      "bus": "на автовокзал приезжайте за 30 минут"}.get(o.transport.mode, "приезжайте заранее")
            items.append((dep.date(), f"выезд в {dep:%H:%M}: {o.title()}. {advice[0].upper() + advice[1:]}"))
        if n.type == "stay" and o and o.hotel and n.check_in:
            items.append((n.check_in, f"заселение в {o.hotel.name}" + (f" с {o.hotel.check_in_time}" if o.hotel.check_in_time else "")))
            if n.check_out:
                items.append((n.check_out, f"выезд из {o.hotel.name}" + (f" до {o.hotel.check_out_time}" if o.hotel.check_out_time else "")))
            if o.rate and o.rate.free_cancellation_until:
                until = o.rate.free_cancellation_until
                items.append((until.date(), f"последний срок бесплатной отмены {o.hotel.name} (до {until:%H:%M})"))
    items.sort(key=lambda x: x[0])
    body = "\n".join(f"• {fmt.day(d)}: {t}" for d, t in items) or "В заказе нет датированных пунктов."
    return (f"{order_line(state)}: ключевые даты.\n{body}\n\nАвтоматические напоминания в этой версии не отправляются: "
            "сохраните даты в календаре.")


def cancellation_note(state: TripState) -> str:
    notes = []
    for _, o in _stays(state):
        if o.rate and o.rate.free_cancellation_until:
            notes.append(f"• {o.hotel.name}: бесплатная отмена до {o.rate.free_cancellation_until:%d.%m %H:%M}")
        elif o.rate and o.rate.free_cancellation is False:
            notes.append(f"• {o.hotel.name}: бесплатной отмены нет")
    return "\n".join(notes)
