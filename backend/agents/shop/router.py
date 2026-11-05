"""Cart, price re-check and simulated payment (nothing is charged or booked)."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from chat import repository as repo
from chat.auth import current_user
from shared.database import get_database, get_session
from shared.events import EventType, OrderPaid, AssistantMessage, emit_event
from shared.models import Cart as CartRow
from shared.models import ChatMessage, Order, User

from ..domain import fmt
from ..domain.cartfp import cart_fingerprint
from ..domain.turn import AgentTurn
from ..runtime.db_store import DbPlanStore
from . import service as svc

log = logging.getLogger("shop")
router = APIRouter(prefix="/api/v1", tags=["shop"])


async def _guard_idle(session: AsyncSession, chat_id: uuid.UUID) -> None:
    if await repo.active_runs(session, chat_id):
        raise HTTPException(409, "Сейчас идёт обработка запроса. Дождитесь ответа и повторите.")


def _fail(e: svc.CartError):
    raise HTTPException(e.status, {"message": e.message, "details": e.details})


def _store(request: Request) -> DbPlanStore:
    return DbPlanStore(get_database())


async def _plan_chat(plan_id: uuid.UUID, user: User, session: AsyncSession) -> uuid.UUID:
    from shared.models import TripPlan
    plan = await session.get(TripPlan, plan_id)
    if plan is None or await repo.get_user_chat(session, plan.chat_id, user.id) is None:
        raise HTTPException(404, "План не найден")
    return plan.chat_id


@router.post("/plans/{plan_id}/cart", status_code=201)
async def create_cart(plan_id: uuid.UUID, request: Request, user: User = Depends(current_user),
                      session: AsyncSession = Depends(get_session)):
    chat_id = await _plan_chat(plan_id, user, session)
    await _guard_idle(session, chat_id)
    store = _store(request)
    state = await store.load(str(chat_id))
    try:
        cart = svc.build_cart(state)
    except svc.CartError as e:
        _fail(e)
    session.add(CartRow(id=uuid.UUID(cart.id), chat_id=chat_id, plan_revision=state.revision, status="draft",
                        items=[i.model_dump(mode="json") for i in cart.items], total=cart.total.amount,
                        currency=cart.total.currency, price_changes=[]))
    await session.commit()
    state = svc.commit_cart(state, cart, "in_cart")
    state = await store.save(str(chat_id), None, state, [], [], "Корзина создана")
    return {"cart": cart.model_dump(mode="json"), "revision": state.revision}


async def _load_cart(session: AsyncSession, cart_id: uuid.UUID, user: User) -> CartRow:
    row = await session.get(CartRow, cart_id)
    if row is None or await repo.get_user_chat(session, row.chat_id, user.id) is None:
        raise HTTPException(404, "Корзина не найдена")
    return row


@router.post("/carts/{cart_id}/check")
async def check(cart_id: uuid.UUID, request: Request, user: User = Depends(current_user),
                session: AsyncSession = Depends(get_session)):
    row = await _load_cart(session, cart_id, user)
    await _guard_idle(session, row.chat_id)
    if row.status == "paid":  # a paid cart is final: it is not re-checked, and it cannot be paid again
        raise HTTPException(409, "Заказ по этой корзине уже оформлен.")
    store = _store(request)
    state = await store.load(str(row.chat_id))
    cart = state.cart
    if cart.id != str(cart_id):
        raise HTTPException(409, "Это не текущая корзина плана: план изменился, соберите корзину заново.")
    try:
        state, changes, gone = await svc.reprice(state, request.app.state.hotels, cart, request.app.state.transport)
    except svc.CartError as e:
        _fail(e)
    state, cart, blockers = svc.check_cart(state, cart, changes, gone)
    state = svc.commit_cart(state, cart, "in_cart")
    state = await store.save(str(row.chat_id), None, state, [], [], "Цены перепроверены")
    row.status, row.items, row.total, row.price_changes = cart.status, [i.model_dump(mode="json") for i in cart.items], cart.total.amount, changes
    row.checked_at = datetime.now(timezone.utc) if cart.status == "checked" else None
    await session.commit()
    return {"cart": cart.model_dump(mode="json"), "price_changes": changes, "blockers": blockers,
            "ready_to_pay": not blockers, "revision": state.revision}


async def _existing_order(session: AsyncSession, row: CartRow) -> dict:
    await session.refresh(row)
    order = await session.scalar(select(Order).where(Order.cart_id == row.id))
    if order is None:
        raise HTTPException(409, "Корзина уже оплачивается: обновите страницу.")
    return {"order_number": order.number, "status": order.status, "total": f"{order.total:.2f}", "currency": order.currency,
            "note": "Оплата имитирована: деньги не списывались", "already_paid": True}


class Guest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)
    phone: str | None = Field(default=None, max_length=40)


class CheckoutIn(BaseModel):
    guest: Guest


@router.post("/carts/{cart_id}/checkout")
async def checkout(cart_id: uuid.UUID, body: CheckoutIn, request: Request, user: User = Depends(current_user),
                   session: AsyncSession = Depends(get_session)):
    row = await _load_cart(session, cart_id, user)
    await _guard_idle(session, row.chat_id)
    if row.status == "paid":  # the same request twice (double click, retry): the order that exists is the answer
        return await _existing_order(session, row)
    if row.status != "checked" or not row.checked_at:
        raise HTTPException(409, "Сначала перепроверьте цены: оплата доступна только для проверенной корзины.")
    checked_at = row.checked_at if row.checked_at.tzinfo else row.checked_at.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - checked_at).total_seconds() / 60
    if age > svc.CHECK_VALID_MINUTES:
        raise HTTPException(409, f"Проверка цен устарела ({int(age)} мин): перепроверьте корзину.")
    store = _store(request)
    state = await store.load(str(row.chat_id))
    if state.cart.id != str(cart_id) or state.cart.status != "checked":
        raise HTTPException(409, "План изменился после проверки: соберите корзину заново.")
    if state.cart.fingerprint != cart_fingerprint(state, state.cart.items):
        raise HTTPException(409, "План или цены изменились после проверки: перепроверьте корзину.")
    if any(state.nodes[i.node_id].status == "stale" for i in state.cart.items if i.node_id in state.nodes):
        raise HTTPException(409, "Часть плана ждёт нового поиска: оплатить сейчас нельзя.")
    number = svc.order_number()
    cart = state.cart.model_copy(update={"status": "paid", "order_number": number})
    # one atomic step: only a cart that is still `checked` can become `paid`, and an order row is unique per cart
    moved = await session.execute(update(CartRow).where(CartRow.id == cart_id, CartRow.status == "checked").values(status="paid"))
    if moved.rowcount != 1:
        await session.rollback()
        return await _existing_order(session, row)
    session.add(Order(number=number, cart_id=cart_id, chat_id=row.chat_id, guest=body.guest.model_dump(),
                      total=row.total, currency=row.currency, status="paid_mock"))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return await _existing_order(session, row)
    state = svc.commit_cart(state, cart, "booked")
    state = await store.save(str(row.chat_id), None, state, [], [], f"Заказ {number}")
    text = (f"Готово! Заказ {number} оформлен на {fmt.rub(cart.total.amount, cart.total.currency)}. "
            "Оплата имитирована: деньги не списывались, брони у поставщиков не создавались.")
    turn = AgentTurn(plan_revision=state.revision, kind="checkout", text=text)
    plan_id = await store.plan_id(str(row.chat_id))
    reply = ChatMessage(chat_id=row.chat_id, role="assistant", text=text, turn=turn.model_dump(mode="json"),
                        plan_id=plan_id, plan_revision=state.revision)
    session.add(reply)
    await session.flush()
    await emit_event(session, row.chat_id, EventType.ORDER_PAID,
                     OrderPaid(order_number=number, total=f"{cart.total.amount:.2f}", currency=cart.total.currency))
    await emit_event(session, row.chat_id, EventType.ASSISTANT_MESSAGE,
                     AssistantMessage(message_id=reply.id, text=text, plan_id=plan_id, revision=state.revision))
    await session.commit()
    return {"order_number": number, "status": "paid_mock", "total": f"{cart.total.amount:.2f}", "currency": cart.total.currency,
            "note": "Оплата имитирована: деньги не списывались", "revision": state.revision}
