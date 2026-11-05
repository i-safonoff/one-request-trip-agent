"""A cart is valid only for the plan it was built from. The fingerprint is the part of the plan a purchase depends on:
which offer is chosen for each node, at what price, for which dates and party, and that the node is not waiting for a
new search. Checking prices changes the plan revision, so the revision cannot serve as the guard; the content can.
"""
from __future__ import annotations

import hashlib
import json

from .models import CartItem, TripState


def cart_fingerprint(state: TripState, items: list[CartItem]) -> str:
    rows = []
    for i in items:
        n, o = state.nodes.get(i.node_id), state.offers.get(i.offer_id)
        rows.append([i.node_id, i.offer_id, f"{o.price.amount:.2f}" if o else None, o.price.currency if o else None,
                     n.status if n else None, n.selected_offer_ref if n else None,
                     str(n.check_in) if n else None, str(n.check_out) if n else None, str(n.departure_date) if n else None])
    rows.append(state.party.sig())
    return hashlib.sha1(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()[:16]
