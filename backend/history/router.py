"""HTTP API of the history module (Mini App and other clients). Identity comes from the chat module's session token."""
import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from chat.auth import current_user
from shared.database import get_session
from shared.models import User
from shared.templates import parse_item

from .service import HistoryError, HistoryService, NotEligible, NotFound
from .texts import describe

router = APIRouter(prefix="/api/v1/history", tags=["history"])


def _svc(session: AsyncSession = Depends(get_session)) -> HistoryService:
    return HistoryService(session)


def _raise(e: HistoryError):
    code = 404 if isinstance(e, NotFound) else 409 if isinstance(e, NotEligible) else 422
    raise HTTPException(code, str(e))


class StayOut(BaseModel):
    node_id: str
    name: str | None
    place_name: str | None
    nights: int | None
    sentiment: int | None


class TripOut(BaseModel):
    chat_id: uuid.UUID
    title: str
    date_from: date | None
    date_to: date | None
    status: str
    is_active: bool
    completed: bool
    can_give_feedback: bool
    trip_sentiment: int | None
    stays: list[StayOut]


class FeedbackIn(BaseModel):
    node_id: str = Field(default="", description="a stay's node id, or '' for the whole trip")
    sentiment: int
    aspects: dict[str, int] = {}
    comment: str | None = Field(default=None, max_length=1000)


class FeedbackOut(BaseModel):
    node_id: str
    sentiment: int
    aspects: dict[str, int]


class ProfileItemOut(BaseModel):
    id: uuid.UUID
    key: str
    text: str
    confidence: float
    observed_trips: int
    muted: bool
    why: list[str]  # the trips behind it, so the user can check it against their own memory


class SuggestionOut(BaseModel):
    id: uuid.UUID
    key: str
    text: str
    reason: str
    status: str


class TemplateIn(BaseModel):
    chat_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    parent_id: uuid.UUID | None = None


class TemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None
    stops: list[str]
    created_at: datetime


class StartIn(BaseModel):
    start_date: date | None = None


def _suggestion(i) -> SuggestionOut:
    return SuggestionOut(id=i.id, key=i.item_key, text=describe(parse_item(i.item)), reason=i.reason, status=i.status)


@router.get("/trips", response_model=list[TripOut])
async def trips(user: User = Depends(current_user), svc: HistoryService = Depends(_svc)):
    return [TripOut(**{**e.__dict__, "stays": [StayOut(**x.__dict__) for x in e.stays]}) for e in await svc.trips(user.id)]


@router.post("/trips/{chat_id}/feedback", response_model=FeedbackOut)
async def give_feedback(chat_id: uuid.UUID, body: FeedbackIn, user: User = Depends(current_user),
                        svc: HistoryService = Depends(_svc)):
    try:
        row = await svc.submit_feedback(user.id, chat_id, body.node_id, body.sentiment, body.aspects, body.comment)
    except HistoryError as e:
        _raise(e)
    return FeedbackOut(node_id=row.node_id, sentiment=row.sentiment, aspects=row.aspects or {})


@router.get("/profile", response_model=list[ProfileItemOut])
async def profile(user: User = Depends(current_user), svc: HistoryService = Depends(_svc)):
    """Everything learned about the user, with its evidence. Transparency is part of the feature."""
    return [ProfileItemOut(id=r.id, key=r.item_key, text=describe(parse_item(r.item)), confidence=r.confidence,
                           observed_trips=r.observed_count, muted=r.muted,
                           why=list(dict.fromkeys(e["trip_title"] for e in r.evidence if e.get("trip_title"))))
            for r in await svc.profile(user.id)]


@router.post("/profile/items/{item_id}/mute", status_code=204)
async def mute(item_id: uuid.UUID, user: User = Depends(current_user), svc: HistoryService = Depends(_svc)):
    try:
        await svc.set_muted(user.id, item_id, True)
    except HistoryError as e:
        _raise(e)


@router.post("/profile/items/{item_id}/unmute", status_code=204)
async def unmute(item_id: uuid.UUID, user: User = Depends(current_user), svc: HistoryService = Depends(_svc)):
    try:
        await svc.set_muted(user.id, item_id, False)
    except HistoryError as e:
        _raise(e)


@router.delete("/profile")
async def forget_everything(user: User = Depends(current_user), svc: HistoryService = Depends(_svc)):
    """Erase feedback, profile, templates and suggestions. The trips themselves stay."""
    return await svc.forget_all(user.id)


@router.post("/chats/{chat_id}/suggestions", response_model=list[SuggestionOut])
async def propose(chat_id: uuid.UUID, place: str | None = None, limit: int = Query(3, ge=1, le=10),
                  user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    svc = HistoryService(session)
    try:
        await svc._chat(user.id, chat_id)  # ownership first
        return [_suggestion(i) for i in await svc.propose(chat_id, place, limit)]
    except HistoryError as e:
        _raise(e)


@router.get("/chats/{chat_id}/suggestions", response_model=list[SuggestionOut])
async def suggestions(chat_id: uuid.UUID, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    svc = HistoryService(session)
    try:
        await svc._chat(user.id, chat_id)
    except HistoryError as e:
        _raise(e)
    return [_suggestion(i) for i in await svc.suggestions(chat_id)]


@router.post("/suggestions/{suggestion_id}/{decision}", response_model=SuggestionOut)
async def decide(suggestion_id: uuid.UUID, decision: str, user: User = Depends(current_user),
                 svc: HistoryService = Depends(_svc)):
    if decision not in ("accept", "dismiss"):
        raise HTTPException(404, "unknown decision")
    row = await svc.decide(user.id, suggestion_id, decision == "accept")
    if row is None:
        raise HTTPException(409, "The suggestion does not exist or was already decided")
    return _suggestion(row)


class IngredientOut(BaseModel):
    key: str
    group: str  # taste | hotel | context
    text: str
    why: str
    default_on: bool
    item: dict | None
    phrase: str | None


class EssenceOut(BaseModel):
    chat_id: uuid.UUID
    title: str
    ingredients: list[IngredientOut]


class BlendPick(BaseModel):
    key: str
    source: uuid.UUID


class BlendIn(BaseModel):
    picks: list[BlendPick] = Field(default=[], max_length=50)


@router.get("/trips/{chat_id}/essence", response_model=EssenceOut)
async def essence(chat_id: uuid.UUID, user: User = Depends(current_user), svc: HistoryService = Depends(_svc)):
    """What a past trip can give to a new one: tastes and hotels (on by default) and context (off by default)."""
    try:
        title, ings = await svc.essence(user.id, chat_id)
    except HistoryError as e:
        _raise(e)
    return EssenceOut(chat_id=chat_id, title=title, ingredients=[IngredientOut(**{
        k: getattr(i, k) for k in IngredientOut.model_fields}) for i in ings])


@router.post("/chats/{chat_id}/blend", response_model=list[SuggestionOut])
async def blend(chat_id: uuid.UUID, body: BlendIn, user: User = Depends(current_user), svc: HistoryService = Depends(_svc)):
    """Store the ingredients picked from past trips as this trip's accepted items (replaces the previous blend).
    The agents apply them with the next message."""
    try:
        rows = await svc.blend(user.id, chat_id, [(p.key, p.source) for p in body.picks])
    except HistoryError as e:
        _raise(e)
    return [_suggestion(r) for r in rows]


def _template(t) -> TemplateOut:
    return TemplateOut(id=t.id, name=t.name, parent_id=t.parent_id, created_at=t.created_at,
                       stops=[s["place_name"] for s in (t.fragment or {}).get("route", [])])


@router.post("/templates", response_model=TemplateOut, status_code=201)
async def save_template(body: TemplateIn, user: User = Depends(current_user), svc: HistoryService = Depends(_svc)):
    try:
        return _template(await svc.save_template(user.id, body.chat_id, body.name, body.parent_id))
    except HistoryError as e:
        _raise(e)


@router.get("/templates", response_model=list[TemplateOut])
async def templates(user: User = Depends(current_user), svc: HistoryService = Depends(_svc)):
    return [_template(t) for t in await svc.templates(user.id)]


@router.get("/templates/{template_id}")
async def resolved_template(template_id: uuid.UUID, user: User = Depends(current_user), svc: HistoryService = Depends(_svc)):
    """The template after `extends` is applied, with where each item came from."""
    try:
        _, res = await svc.resolved(user.id, template_id)
    except HistoryError as e:
        _raise(e)
    return res.model_dump()


@router.post("/templates/{template_id}/start")
async def start_from_template(template_id: uuid.UUID, body: StartIn, user: User = Depends(current_user),
                              svc: HistoryService = Depends(_svc)):
    """Start a new trip shaped like the template (same route, no offers or prices)."""
    try:
        chat, plan = await svc.start_from_template(user.id, template_id, body.start_date)
    except HistoryError as e:
        _raise(e)
    return {"chat_id": str(chat.id), "plan_id": str(plan.id), "title": plan.state_json["title"]}
