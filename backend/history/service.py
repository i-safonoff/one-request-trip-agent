"""Trip history use cases: feedback, the learned profile, suggestions for a new trip, templates.

Every method commits. The module reads plans from `trip_plans` but never writes them, except for the draft that
`start_from_template` creates for a brand-new trip. It talks to the agents only through `inherited_items`.
"""
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.plan_view import with_defaults
from shared.config import Settings, get_settings
from shared.events import EventType, SuggestionsProposed, SuggestionView, emit_event
from shared.models import Chat, InheritedItem, TripPlan, TripPlanVersion, User
from shared.templates import ASPECTS, HotelHint, parse_item

from . import facts as plan_facts
from .aggregation import aggregate
from .essence import CONTEXT, Ingredient, essence as essence_of
from .extraction import Candidate, extract
from .models import PreferenceItem, TripFeedback, TripTemplate
from .skeleton import ResolvedFragment, TemplateFragment, instantiate, resolve, skeleton_from_state
from .texts import describe, reason


class HistoryError(Exception):
    pass


class NotFound(HistoryError):
    pass


class NotEligible(HistoryError):
    """Feedback on a trip that has not happened yet: nobody can rate a stay they haven't had."""


class Invalid(HistoryError):
    pass


BLEND_REASON = "Из поездк"  # «Из поездки …» / «Из поездок …»: marks rows written by a blend, so the next blend can replace them


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class StayRef:
    node_id: str
    name: str | None
    place_name: str | None
    nights: int | None
    sentiment: int | None = None  # the user's feedback on this stay, if any


@dataclass
class TripEntry:
    chat_id: uuid.UUID
    title: str
    date_from: date | None
    date_to: date | None
    status: str
    is_active: bool
    completed: bool
    can_give_feedback: bool
    stays: list[StayRef] = field(default_factory=list)
    trip_sentiment: int | None = None


class HistoryService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None):
        self.session = session
        self.settings = settings or get_settings()

    # ---- helpers ----
    async def _chat(self, user_id: uuid.UUID, chat_id: uuid.UUID) -> Chat:
        chat = await self.session.scalar(select(Chat).where(Chat.id == chat_id, Chat.user_id == user_id))
        if chat is None:
            raise NotFound("trip not found")  # also for someone else's trip
        return chat

    async def _plan(self, chat_id: uuid.UUID) -> TripPlan | None:
        return await self.session.scalar(
            select(TripPlan).where(TripPlan.chat_id == chat_id).order_by(TripPlan.updated_at.desc()))

    @staticmethod
    def _state(plan: TripPlan) -> dict:
        return plan.state_json or plan.view_json or {}

    def _can_give_feedback(self, completed: bool) -> bool:
        return completed or self.settings.history_allow_early_feedback

    # ---- history ----
    async def trips(self, user_id: uuid.UUID, today: date | None = None) -> list[TripEntry]:
        """The user's trips that have a plan with at least one chosen stay, newest first."""
        today = today or date.today()
        chats = await self.session.scalars(select(Chat).where(Chat.user_id == user_id).order_by(Chat.created_at.desc()))
        entries = []
        for chat in chats:
            plan = await self._plan(chat.id)
            if plan is None:
                continue
            state = self._state(plan)
            stays = plan_facts.stays(state)
            if not stays:
                continue
            d_from, d_to = plan_facts.date_range(state)
            completed = d_to is not None and d_to <= today
            feedback = {f.node_id: f.sentiment for f in await self.session.scalars(
                select(TripFeedback).where(TripFeedback.chat_id == chat.id))}
            entries.append(TripEntry(
                chat_id=chat.id, title=state.get("title") or "Поездка", date_from=d_from, date_to=d_to,
                status=plan.status, is_active=chat.status == "active", completed=completed,
                can_give_feedback=self._can_give_feedback(completed), trip_sentiment=feedback.get(""),
                stays=[StayRef(f.node_id, f.name, f.place_name, f.nights, feedback.get(f.node_id)) for f in stays]))
        return entries

    # ---- feedback ----
    async def submit_feedback(self, user_id: uuid.UUID, chat_id: uuid.UUID, node_id: str, sentiment: int,
                              aspects: dict[str, int] | None = None, comment: str | None = None,
                              today: date | None = None) -> TripFeedback:
        today = today or date.today()
        chat = await self._chat(user_id, chat_id)
        plan = await self._plan(chat.id)
        if plan is None:
            raise NotFound("the trip has no plan")
        state = self._state(plan)
        stay_ids = {f.node_id for f in plan_facts.stays(state)}
        if not stay_ids:
            raise NotFound("the trip has no chosen stays to rate")
        _, d_to = plan_facts.date_range(state)
        if not self._can_give_feedback(d_to is not None and d_to <= today):
            raise NotEligible("Оценить можно после поездки.")
        if sentiment not in (-1, 0, 1):
            raise Invalid("sentiment must be -1, 0 or 1")
        aspects = aspects or {}
        if any(a not in ASPECTS for a in aspects) or any(v not in (-1, 1) for v in aspects.values()):
            raise Invalid(f"aspects must be a subset of {list(ASPECTS)} with values -1 or 1")
        if node_id and node_id not in stay_ids:
            raise Invalid("unknown stay")
        row = await self.session.scalar(select(TripFeedback).where(
            TripFeedback.chat_id == chat.id, TripFeedback.node_id == node_id))
        if row is None:
            row = TripFeedback(user_id=user_id, chat_id=chat.id, node_id=node_id, sentiment=sentiment)
            self.session.add(row)
        row.sentiment, row.aspects, row.updated_at = sentiment, aspects, _now()
        row.comment = (comment or "").strip()[:1000] or None
        await self.session.flush()
        await self.rebuild_profile(user_id)
        await self.session.commit()
        return row

    # ---- profile ----
    async def rebuild_profile(self, user_id: uuid.UUID, now: datetime | None = None) -> list[PreferenceItem]:
        """Recompute the profile from all feedback. Idempotent: the profile is a function of the evidence."""
        now = now or _now()
        feedback = list(await self.session.scalars(select(TripFeedback).where(TripFeedback.user_id == user_id)))
        by_chat: dict[uuid.UUID, list[TripFeedback]] = {}
        for fb in feedback:
            by_chat.setdefault(fb.chat_id, []).append(fb)

        candidates: list[Candidate] = []
        for chat_id, rows in by_chat.items():
            plan = await self._plan(chat_id)
            if plan is None:
                continue
            state = self._state(plan)
            stays = {f.node_id: f for f in plan_facts.stays(state)}
            title = state.get("title") or "поездка"
            rated_nodes = {r.node_id for r in rows if r.node_id}
            for fb in rows:
                targets = [(stays[fb.node_id], 1.0)] if fb.node_id and fb.node_id in stays else \
                    [(f, 0.5) for nid, f in stays.items() if nid not in rated_nodes] if not fb.node_id else []
                for f, weight in targets:
                    evidence = {"chat_id": str(chat_id), "node_id": f.node_id, "feedback_id": str(fb.id),
                                "trip_title": title, "hotel_name": f.name, "at": _utc(fb.updated_at).isoformat()}
                    candidates += extract(f, fb.sentiment, fb.aspects or {}, evidence, weight)

        aggregated = aggregate(candidates, now, self.settings.history_half_life_days)
        existing = {r.item_key: r for r in await self.session.scalars(
            select(PreferenceItem).where(PreferenceItem.user_id == user_id))}
        keep = set()
        for a in aggregated:
            keep.add(a.key)
            row = existing.get(a.key) or PreferenceItem(user_id=user_id, item_key=a.key)
            if a.key not in existing:
                self.session.add(row)
            row.item = a.item.model_dump(by_alias=True)
            row.confidence, row.observed_count, row.evidence, row.updated_at = a.confidence, a.observed, a.evidence[-20:], now
        for key, row in existing.items():
            if key not in keep:
                await self.session.delete(row)  # its evidence is gone (feedback changed or deleted)
        await self.session.flush()
        return await self.profile(user_id)

    async def profile(self, user_id: uuid.UUID) -> list[PreferenceItem]:
        return list(await self.session.scalars(
            select(PreferenceItem).where(PreferenceItem.user_id == user_id).order_by(
                PreferenceItem.confidence.desc(), PreferenceItem.item_key)))

    async def set_muted(self, user_id: uuid.UUID, item_id: uuid.UUID, muted: bool) -> PreferenceItem:
        row = await self.session.scalar(select(PreferenceItem).where(
            PreferenceItem.id == item_id, PreferenceItem.user_id == user_id))
        if row is None:
            raise NotFound("profile item not found")
        row.muted = muted
        await self.session.commit()
        return row

    async def forget_all(self, user_id: uuid.UUID) -> dict[str, int]:
        """Delete everything this module knows about the user (feedback, profile, templates, suggestions).
        The trips themselves (chats and plans) belong to the chat/agents modules and are not touched."""
        counts = {}
        for name, model in (("suggestions", InheritedItem), ("profile", PreferenceItem), ("feedback", TripFeedback)):
            counts[name] = (await self.session.execute(delete(model).where(model.user_id == user_id))).rowcount
        # templates may extend each other: children first
        await self.session.execute(delete(TripTemplate).where(TripTemplate.user_id == user_id, TripTemplate.parent_id.is_not(None)))
        counts["templates"] = (await self.session.execute(delete(TripTemplate).where(TripTemplate.user_id == user_id))).rowcount
        await self.session.commit()
        return counts

    # ---- blending past trips into a new one (the drag-and-drop screen) ----
    async def essence(self, user_id: uuid.UUID, chat_id: uuid.UUID) -> tuple[str, list[Ingredient]]:
        """What this past trip can give to a new one. Someone else's trip is NotFound."""
        await self._chat(user_id, chat_id)
        plan = await self._plan(chat_id)
        if plan is None:
            raise NotFound("trip has no plan")
        state = self._state(plan)
        liked = [r.item for r in await self.profile(user_id)
                 if not r.muted and any(e.get("chat_id") == str(chat_id) for e in r.evidence or [])]
        return state.get("title") or "Поездка", essence_of(state, liked)

    async def blend(self, user_id: uuid.UUID, chat_id: uuid.UUID, picks: list[tuple[str, uuid.UUID]]) -> list[InheritedItem]:
        """Make the picked ingredients the new trip's accepted items. `picks` are (key, source trip); the item is
        re-read from the source on the server, the client only chooses. Context is not stored here: it travels as
        text in the user's next message. A blend replaces the previous one: keys it no longer has are dismissed.
        Items already applied to the plan stay there (the agents apply each accepted item once)."""
        chat = await self._chat(user_id, chat_id)
        sources: dict[uuid.UUID, tuple[str, dict[str, Ingredient]]] = {}
        for _, src in picks:
            if src not in sources:
                title, ings = await self.essence(user_id, src)
                sources[src] = (title, {i.key: i for i in ings})
        chosen: dict[str, tuple[dict, list[str]]] = {}
        for key, src in picks:
            ing = sources[src][1].get(key)
            if ing is None or ing.group == CONTEXT:
                raise Invalid(f"{key} is not an ingredient of that trip")
            titles = [t for t, ings in sources.values() if ings.get(key) and ings[key].item == ing.item]
            chosen[key] = (ing.item, titles)
        rows = {r.item_key: r for r in await self.session.scalars(select(InheritedItem).where(InheritedItem.chat_id == chat_id))}
        out = []
        for key, (item, titles) in chosen.items():
            row = rows.get(key) or InheritedItem(chat_id=chat_id, user_id=chat.user_id, item_key=key)
            if key not in rows:
                self.session.add(row)
            row.item, row.status, row.decided_at, row.confidence = item, "accepted", _now(), 1.0
            row.reason = ("Из поездки " if len(titles) == 1 else "Из поездок ") + ", ".join(f"«{t}»" for t in titles)
            out.append(row)
        for key, row in rows.items():
            if key not in chosen and row.reason.startswith(BLEND_REASON) and row.status == "accepted":
                row.status, row.decided_at = "dismissed", _now()
        await self.session.commit()
        return out

    # ---- suggestions for a new trip ----
    async def propose(self, chat_id: uuid.UUID, place_name: str | None = None, limit: int = 3) -> list[InheritedItem]:
        """Offer up to `limit` things from past trips for this (new) trip. Each key is offered once per trip, so a
        dismissed suggestion does not come back. Nothing is applied until the user accepts it."""
        chat = await self.session.get(Chat, chat_id)
        if chat is None:
            raise NotFound("chat not found")
        rows = [r for r in await self.profile(chat.user_id)
                if not r.muted and r.confidence >= self.settings.history_min_confidence]
        seen = set(await self.session.scalars(select(InheritedItem.item_key).where(InheritedItem.chat_id == chat_id)))
        created = []
        for r in rows:
            item = parse_item(r.item)
            if r.item_key in seen:
                continue
            if isinstance(item, HotelHint) and not (place_name and item.place_name
                                                    and item.place_name.lower() == place_name.lower()):
                continue  # a hotel hint is only relevant for the same destination
            titles = list(dict.fromkeys(e.get("trip_title") for e in r.evidence if e.get("trip_title")))
            created.append(InheritedItem(chat_id=chat_id, user_id=chat.user_id, item_key=r.item_key, item=r.item,
                                         reason=reason(titles, r.observed_count), confidence=r.confidence))
            if len(created) >= limit:
                break
        self.session.add_all(created)
        await self.session.flush()
        if created:
            await emit_event(self.session, chat_id, EventType.SUGGESTIONS_PROPOSED, SuggestionsProposed(
                intro="Учитывая ваши прошлые поездки, могу сразу учесть:",
                items=[SuggestionView(suggestion_id=c.id, text=describe(parse_item(c.item))) for c in created]))
        await self.session.commit()
        return created

    async def decide(self, user_id: uuid.UUID, suggestion_id: uuid.UUID, accept: bool) -> InheritedItem | None:
        """Accept or dismiss a proposed item. None if it is unknown, not the user's, or already decided."""
        row = await self.session.scalar(select(InheritedItem).where(
            InheritedItem.id == suggestion_id, InheritedItem.user_id == user_id, InheritedItem.status == "proposed"))
        if row is None:
            return None
        row.status, row.decided_at = ("accepted" if accept else "dismissed"), _now()
        await self.session.flush()
        if not accept:  # dismissed twice for the same thing, across trips: stop suggesting it
            dismissals = await self.session.scalar(select(func.count()).select_from(InheritedItem).where(
                InheritedItem.user_id == user_id, InheritedItem.item_key == row.item_key,
                InheritedItem.status == "dismissed"))
            if dismissals >= 2:
                pref = await self.session.scalar(select(PreferenceItem).where(
                    PreferenceItem.user_id == user_id, PreferenceItem.item_key == row.item_key))
                if pref:
                    pref.muted = True
        await self.session.commit()
        return row

    async def accepted(self, chat_id: uuid.UUID) -> list[tuple[dict, str, str]]:
        """What the agents apply: [(item, reason, id)] for shared.templates.apply_items."""
        rows = await self.session.scalars(select(InheritedItem).where(
            InheritedItem.chat_id == chat_id, InheritedItem.status == "accepted").order_by(InheritedItem.created_at))
        return [(r.item, r.reason, str(r.id)) for r in rows]

    async def suggestions(self, chat_id: uuid.UUID) -> list[InheritedItem]:
        return list(await self.session.scalars(
            select(InheritedItem).where(InheritedItem.chat_id == chat_id).order_by(InheritedItem.created_at)))

    # ---- templates ----
    async def save_template(self, user_id: uuid.UUID, chat_id: uuid.UUID, name: str,
                            parent_id: uuid.UUID | None = None) -> TripTemplate:
        chat = await self._chat(user_id, chat_id)
        plan = await self._plan(chat.id)
        if plan is None:
            raise NotFound("the trip has no plan")
        name = name.strip()[:120]
        if not name:
            raise Invalid("a template needs a name")
        fragment = skeleton_from_state(self._state(plan))
        if not fragment.route and not fragment.legs:
            raise Invalid("the trip has no route to save")
        if parent_id is not None:
            parent = await self._template(user_id, parent_id)
            if parent.parent_id is not None:
                raise Invalid("a template can extend only a template that extends nothing (depth 1)")
        row = TripTemplate(user_id=user_id, name=name, source_chat_id=chat.id, parent_id=parent_id,
                           fragment=fragment.model_dump(), updated_at=_now())
        self.session.add(row)
        await self.session.commit()
        return row

    async def _template(self, user_id: uuid.UUID, template_id: uuid.UUID) -> TripTemplate:
        row = await self.session.scalar(select(TripTemplate).where(
            TripTemplate.id == template_id, TripTemplate.user_id == user_id))
        if row is None:
            raise NotFound("template not found")
        return row

    async def template_for_trip(self, user_id: uuid.UUID, chat_id: uuid.UUID) -> TripTemplate:
        """The template made from this trip, created on first use ("repeat this trip")."""
        existing = await self.session.scalar(select(TripTemplate).where(
            TripTemplate.user_id == user_id, TripTemplate.source_chat_id == chat_id, TripTemplate.parent_id.is_(None)))
        if existing:
            return existing
        chat = await self._chat(user_id, chat_id)
        plan = await self._plan(chat.id)
        title = self._state(plan).get("title") if plan else None
        return await self.save_template(user_id, chat_id, title or "Поездка")

    async def templates(self, user_id: uuid.UUID) -> list[TripTemplate]:
        return list(await self.session.scalars(
            select(TripTemplate).where(TripTemplate.user_id == user_id).order_by(TripTemplate.created_at.desc())))

    async def resolved(self, user_id: uuid.UUID, template_id: uuid.UUID) -> tuple[TripTemplate, ResolvedFragment]:
        row = await self._template(user_id, template_id)
        parent = TemplateFragment.model_validate((await self._template(user_id, row.parent_id)).fragment) \
            if row.parent_id else None
        return row, resolve(TemplateFragment.model_validate(row.fragment), parent)

    async def start_from_template(self, user_id: uuid.UUID, template_id: uuid.UUID,
                                  start: date | None = None) -> tuple[Chat, TripPlan]:
        """Begin a new trip shaped like the template: a draft plan with the same route, no offers, no prices."""
        template, res = await self.resolved(user_id, template_id)
        user = await self.session.get(User, user_id)
        for old in await self.session.scalars(select(Chat).where(Chat.user_id == user_id, Chat.status == "active")):
            old.status = "archived"
        chat = Chat(user_id=user_id, telegram_chat_id=user.telegram_id)  # a private chat's id is the user's id
        self.session.add(chat)
        await self.session.flush()
        draft = instantiate(res.fragment, template.name, start)
        view = with_defaults(draft)  # the web UI reads the full view shape (pricing, checks, ...), not only the map part
        plan = TripPlan(chat_id=chat.id, revision=1, status="draft", state_json=draft, view_json=view)
        self.session.add(plan)
        await self.session.flush()
        self.session.add(TripPlanVersion(plan_id=plan.id, revision=1, state_json=draft, view_json=view))
        await self.session.commit()
        return chat, plan
