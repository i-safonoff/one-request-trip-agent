"""One adapter for every writer of `trip_plans.view_json`.

The agents write the full view (agents/domain/view.py). A plan created elsewhere (a draft started from a trip
template, the dev/demo agent) holds only the map part. The web UI reads the full shape, so such a view is completed
with empty defaults on the way out instead of every reader guarding every field.
"""
from __future__ import annotations

from typing import Any

VIEW_DEFAULTS: dict[str, Any] = {
    "status": "draft", "title": None, "trip": {}, "budget": {}, "party": {}, "priorities": [], "constraints": [],
    "understood": [], "places": {}, "sequence": [], "nodes": {}, "offers": {}, "checks": [], "data_notes": [],
    "questions": [], "destination_candidates": [], "cart": {"status": "empty", "items": []},
    "pricing": {"currency": "RUB", "known_subtotal": "0.00", "total": None, "is_complete": False, "missing": [],
                "excluded": [], "budget_scoped_total": "0.00", "within_budget": None},
}


def with_defaults(view: dict | None) -> dict:
    out = {**{k: (v.copy() if isinstance(v, (dict, list)) else v) for k, v in VIEW_DEFAULTS.items()}, **(view or {})}
    for key in ("pricing", "cart"):  # a present but partial block still gets the fields readers rely on
        if isinstance(view, dict) and isinstance(view.get(key), dict):
            out[key] = {**VIEW_DEFAULTS[key], **view[key]}
    nodes = {}
    for nid, n in (out.get("nodes") or {}).items():
        n = dict(n)
        n.setdefault("label", {"stay": "Проживание", "transport": "Переезд", "transfer": "Трансфер"}.get(n.get("type"), nid))
        n.setdefault("options", [])
        n.setdefault("option_ids", [])
        nodes[nid] = n
    out["nodes"] = nodes
    return out
