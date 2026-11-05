"""Cart and simulated checkout. Nothing here touches a real payment or a real booking.

`check` re-prices every item at the source and re-runs the verifier in strict mode (stale offers
are errors). `checkout` only works on a cart that passed `check` a moment ago.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from ..domain.apply import apply_ops
from ..domain.cartfp import cart_fingerprint
from ..domain.checks import run_checks
from ..domain.models import Cart, CartItem, Money, TripState
from ..domain.ops import SetCart, SetChecks, SetStatus
from ..domain.turn import risk_text
from ..integrations.hotels import HotelQuery, TutuHotels
from ..integrations.transport import TransportQuery, TutuTransport
from ..integrations.mcp_client import McpError

CHECK_VALID_MINUTES = 15


class CartError(Exception):
    def __init__(self, message: str, details: list | None = None, status: int = 409):
        super().__init__(message)
        self.message, self.details, self.status = message, details or [], status


def priced_nodes(state: TripState):
    scope = state.scope or "full"
    return [state.nodes[i] for i in state.sequence if state.nodes[i].type != "transfer" and (
        scope == "full" or (scope == "stay" and state.nodes[i].type == "stay") or (scope == "transport" and state.nodes[i].type == "transport"))]


def build_cart(state: TripState) -> Cart:
    nodes = priced_nodes(state)
    missing = [n.id for n in nodes if n.status == "stale" or not (n.selected_offer_ref and n.selected_offer_ref in state.offers)]
    if not nodes or missing:
        raise CartError("В плане есть части без выбранного варианта: корзину собрать нельзя.", [{"node_id": m} for m in missing])
    items = [CartItem(node_id=n.id, offer_id=n.selected_offer_ref, title=state.offers[n.selected_offer_ref].title(),
                      price=state.offers[n.selected_offer_ref].price) for n in nodes]
    cur = items[0].price.currency
    total = sum((i.price.amount for i in items), Decimal(0))
    return Cart(id=str(uuid.uuid4()), status="draft", items=items, total=Money(amount=total, currency=cur, basis="cart_total"),
                fingerprint=cart_fingerprint(state, items))


async def reprice(state: TripState, hotels, cart: Cart, transport=None) -> tuple[TripState, list[dict], list[dict]]:
    """Fresh prices from the source. Returns (state with updated offers, price changes, unavailable items)."""
    changes: list[dict] = []
    gone: list[dict] = []
    now = datetime.now(timezone.utc)
    for item in cart.items:
        offer = state.offers.get(item.offer_id)
        node = state.nodes.get(item.node_id)
        if not offer or not node:
            gone.append({"node_id": item.node_id, "reason": "предложение больше не в плане"})
            continue
        if offer.source == "tutu_mcp" and offer.kind == "hotel_rate" and isinstance(hotels, TutuHotels):
            q = HotelQuery(city="", check_in=node.check_in, check_out=node.check_out, adults=state.party.adults or 1,
                           children_ages=list(state.party.children_ages))
            try:
                fresh = await hotels.reprice(offer, q)
            except McpError as e:
                raise CartError(f"Не удалось перепроверить цену у источника: {e}", status=503)
            if fresh is None:
                gone.append({"node_id": item.node_id, "reason": "тариф больше недоступен"})
                continue
            if fresh.price.amount != offer.price.amount:
                changes.append({"node_id": item.node_id, "title": offer.title(), "old": str(offer.price.amount),
                                "new": str(fresh.price.amount), "currency": offer.price.currency})
            state.offers[offer.id] = fresh
        elif offer.source == "tutu_mcp" and offer.kind == "transport" and isinstance(transport, TutuTransport):
            o, d = state.places.get(node.from_ or ""), state.places.get(node.to or "")
            q = TransportQuery(o, d, node.departure_date, state.party.adults or 1, len(state.party.children_ages) + state.party.children_unknown)
            try:
                fresh = await transport.reprice(offer, q)
            except McpError as e:
                raise CartError(f"Не удалось перепроверить цену у источника: {e}", status=503)
            if fresh is None:
                gone.append({"node_id": item.node_id, "reason": "рейс или поезд больше недоступен"})
                continue
            if fresh.price.amount != offer.price.amount:
                changes.append({"node_id": item.node_id, "title": offer.title(), "old": str(offer.price.amount),
                                "new": str(fresh.price.amount), "currency": offer.price.currency})
            state.offers[offer.id] = fresh
        else:  # demo data: nothing to re-fetch, just refresh the timestamp
            offer.retrieved_at = now
    return state, changes, gone


def check_cart(state: TripState, cart: Cart, changes: list[dict], gone: list[dict]) -> tuple[TripState, Cart, list[dict]]:
    """Re-run the verifier in strict mode; returns blocking problems (empty list = ready to pay)."""
    blockers: list[dict] = [{"code": "UNAVAILABLE", "text": g["reason"], "node_id": g["node_id"]} for g in gone]
    items = [CartItem(node_id=i.node_id, offer_id=i.offer_id, title=i.title, price=state.offers[i.offer_id].price)
             for i in cart.items if i.offer_id in state.offers]
    total = sum((i.price.amount for i in items), Decimal(0))
    cart = cart.model_copy(update={"items": items, "price_changes": changes,
                                   "total": Money(amount=total, currency=cart.total.currency if cart.total else "RUB", basis="cart_total")})
    checks = run_checks(state, strict_stale=True)
    res = apply_ops(state, "verifier", [SetChecks(checks=checks)])
    state = res.state
    blockers += [{"code": c.code, "text": risk_text(c), "node_ids": c.node_ids} for c in checks if c.severity == "error"]
    cart.status = "draft" if blockers else "checked"
    cart.fingerprint = cart_fingerprint(state, cart.items)  # prices may have moved: the cart now stands for THIS state
    return state, cart, blockers


def commit_cart(state: TripState, cart: Cart, plan_status: str) -> TripState:
    res = apply_ops(state, "orchestrator", [SetCart(cart=cart.model_dump(mode="json")), SetStatus(status=plan_status)])  # type: ignore[arg-type]
    return res.state


def order_number() -> str:
    return "EKV-" + uuid.uuid4().hex[:6].upper()
