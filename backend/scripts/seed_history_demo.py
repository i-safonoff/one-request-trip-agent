"""Seed past trips with feedback for the blend demo ("drag Japan into China").

Creates three finished trips for one user (Japan, Turkey, Kazan) with the criteria the user chose, who travelled,
the budget and a rating, then rebuilds the learned profile. Idempotent per title: running it twice does not
duplicate trips.

    python scripts/seed_history_demo.py --user-id <uuid>      # an existing user (e.g. your guest session)
    python scripts/seed_history_demo.py --telegram-id 123     # a Telegram user, created if missing
Reads DATABASE_URL like the app.
"""
import argparse
import asyncio
import copy
import json
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from history.service import HistoryService  # noqa: E402
from shared.database import get_database  # noqa: E402
from shared.models import Chat, TripPlan, User  # noqa: E402

JAPAN = json.loads((Path(__file__).resolve().parent.parent / "trip_map" / "examples" / "trip.json").read_text("utf-8"))


def _shift(state: dict, days: int) -> dict:
    """Move every date of a plan by `days` (the example trip is in the future; history needs past trips)."""
    out = copy.deepcopy(state)
    for n in out["nodes"].values():
        for k in ("check_in", "check_out", "departure_date"):
            if n.get(k):
                n[k] = (date.fromisoformat(n[k]) + timedelta(days=days)).isoformat()
    return out


def _city_trip(title: str, city: str, lat: float, lon: float, start: date, nights: int, hotel: str, amenities: list[str]) -> dict:
    end = start + timedelta(days=nights)
    return {"title": title, "status": "ready", "sequence": ["stay"],
            "places": {"city": {"name": city, "lat": lat, "lon": lon}},
            "nodes": {"stay": {"type": "stay", "place": "city", "check_in": start.isoformat(), "check_out": end.isoformat(),
                               "selected_offer_ref": "hotel"}},
            "offers": {"hotel": {"name": hotel, "stars": 4, "rating": 8.9, "amenities": amenities, "price": 90000, "currency": "RUB"}}}


TRIPS = [
    ({**_shift(JAPAN, -210), "title": "Япония",
      "constraints": [{"kind": "free_cancellation", "value": True}, {"kind": "min_rating", "value": 8.5},
                      {"kind": "exclude_modes", "value": ["bus"]}],
      "priorities": [{"key": "location", "weight": 2.5}, {"key": "transfers", "weight": 1.5}],
      "party": {"adults": 2}, "budget": {"amount": "450000", "currency": "RUB", "scope": "trip"}},
     [("stay_osaka", 1, {"location": 1, "rating": 1})]),
    ({**_city_trip("Турция", "Анталья", 36.8969, 30.7133, date(2025, 8, 10), 7, "Akra Hotel", ["завтрак", "бесплатная отмена", "бассейн"]),
      "constraints": [{"kind": "breakfast", "value": True}, {"kind": "free_cancellation", "value": True},
                      {"kind": "min_stars", "value": 4}, {"kind": "kid_friendly", "value": True}],
      "priorities": [{"key": "price", "weight": 1.5}, {"key": "meal", "weight": 2.0}],
      "party": {"adults": 2, "children_ages": [6]}, "budget": {"amount": "280000", "currency": "RUB", "scope": "trip"}},
     [("stay", 1, {"meal": 1, "price": 1})]),
    ({**_city_trip("Казань", "Казань", 55.7963, 49.1088, date(2026, 5, 1), 3, "Courtyard Kazan Kremlin", ["завтрак"]),
      "constraints": [{"kind": "breakfast", "value": True}, {"kind": "min_stars", "value": 3}],
      "priorities": [{"key": "location", "weight": 2.0}],
      "party": {"adults": 1}, "budget": {"amount": "40000", "currency": "RUB", "scope": "stay"}},
     [("stay", 1, {"location": 1})]),
]


async def main(user_id: uuid.UUID | None, telegram_id: int | None) -> None:
    db = get_database()
    await db.create_tables()
    async with db.sessionmaker() as s:
        if user_id is None:
            user = await s.scalar(select(User).where(User.telegram_id == telegram_id))
            if user is None:
                user = User(telegram_id=telegram_id, first_name="Демо")
                s.add(user)
                await s.commit()
            user_id = user.id
        elif await s.get(User, user_id) is None:
            raise SystemExit(f"no user {user_id}")
        for state, ratings in TRIPS:
            exists = await s.scalar(select(TripPlan).join(Chat, Chat.id == TripPlan.chat_id).where(
                Chat.user_id == user_id, TripPlan.state_json["title"].as_string() == state["title"]))
            if exists:
                print(f"{state['title']}: already there")
                continue
            chat = Chat(user_id=user_id, status="archived")
            s.add(chat)
            await s.flush()
            s.add(TripPlan(chat_id=chat.id, revision=1, status="ready", state_json=state, view_json=state))
            await s.commit()
            svc = HistoryService(s)
            for node, sentiment, aspects in ratings:
                await svc.submit_feedback(user_id, chat.id, node, sentiment, aspects, None, today=date(2027, 1, 1))
            print(f"{state['title']}: chat {chat.id}")
        await HistoryService(s).rebuild_profile(user_id)
        await s.commit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--user-id", type=uuid.UUID)
    g.add_argument("--telegram-id", type=int)
    a = ap.parse_args()
    asyncio.run(main(a.user_id, a.telegram_id))
