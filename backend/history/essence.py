"""What a past trip can give to a new one: its "ingredients", for the drag-and-blend screen.

Three groups, because they deserve different defaults:
  * taste   - criteria the user chose in that trip or liked after it (breakfast, free cancellation, location first).
              On by default: this is what "like Japan, but China" means.
  * hotel   - a hotel they liked or want to avoid. On by default, but only a hint: it applies to the same city only.
  * context - who travelled, the budget, how long. Off by default: a family trip is not a business trip, and
              silently copying context is the classic way to get a wrong plan. The user can switch it on; it is then
              sent as editable text in the next message, not as a hidden structured value.
Pure functions: the service reads the plan and the profile, this module only decides.
"""
from dataclasses import dataclass, field

from shared.templates import ConstraintItem, HotelHint, PriorityItem, parse_item

from . import facts as plan_facts
from .skeleton import skeleton_from_state
from .texts import describe

TASTE, HOTEL, CONTEXT = "taste", "hotel", "context"


@dataclass
class Ingredient:
    key: str  # item key ("constraint:breakfast") or "context:<what>"; equal keys merge across trips
    group: str
    text: str
    why: str
    default_on: bool
    item: dict | None = None  # shared.templates item, for taste / hotel
    phrase: str | None = None  # for context: what goes into the user's next message
    sources: list[str] = field(default_factory=list)


def _rub(amount) -> str:
    try:
        return f"{int(float(amount)):,}".replace(",", " ") + " ₽"
    except (TypeError, ValueError):
        return str(amount)


def _years(n: int) -> str:
    word = "год" if n % 10 == 1 and n % 100 != 11 else "года" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "лет"
    return f"{n} {word}"


def _party(party: dict) -> tuple[str, str] | None:
    adults, kids = party.get("adults"), party.get("children_ages") or []
    if not adults:
        return None
    text = f"{adults} взросл{'ый' if adults == 1 else 'ых'}"
    if kids:
        text += (", ребёнок " if len(kids) == 1 else ", дети ") + ", ".join(_years(a) for a in kids)
    return text, f"нас {text}"


def context_of(state: dict) -> list[Ingredient]:
    title = state.get("title") or "поездка"
    out: list[Ingredient] = []
    if (p := _party(state.get("party") or {})):
        out.append(Ingredient("context:party", CONTEXT, p[0], f"состав в «{title}»", False, phrase=p[1]))
    budget = state.get("budget") or {}
    if budget.get("amount"):
        scope = {"stay": "на проживание", "transport": "на дорогу"}.get(budget.get("scope"), "на поездку")
        text = f"до {_rub(budget['amount'])} {scope}"
        out.append(Ingredient("context:budget", CONTEXT, text, f"бюджет в «{title}»", False, phrase=f"бюджет {text}"))
    nights = sum(s.nights or 0 for s in plan_facts.stays(state))
    if nights:
        word = "ночь" if nights % 10 == 1 and nights % 100 != 11 else "ночи" if nights % 10 in (2, 3, 4) and nights % 100 not in (12, 13, 14) else "ночей"
        out.append(Ingredient("context:nights", CONTEXT, f"{nights} {word}", f"длительность «{title}»", False,
                              phrase=f"примерно на {nights} {word}"))
    return out


def essence(state: dict, liked: list[dict]) -> list[Ingredient]:
    """Ingredients of one trip. `liked` are profile items whose evidence includes this trip (from feedback)."""
    title = state.get("title") or "поездка"
    by_key: dict[str, Ingredient] = {}
    sk = skeleton_from_state(state)
    for item in [*sk.constraints, *sk.priorities]:
        by_key[item.key] = Ingredient(item.key, TASTE, describe(item), f"вы выбирали в «{title}»", True,
                                      item=item.model_dump(by_alias=True))
    for raw in liked:
        try:
            item = parse_item(raw)
        except ValueError:
            continue
        if isinstance(item, HotelHint):
            why = f"из «{title}», сработает только в {item.place_name}" if item.place_name else f"из «{title}»"
            by_key[item.key] = Ingredient(item.key, HOTEL, describe(item), why, True, item=item.model_dump(by_alias=True))
        elif isinstance(item, (ConstraintItem, PriorityItem)):
            known = by_key.get(item.key)
            if known and known.item == item.model_dump(by_alias=True):
                known.why = f"выбирали в «{title}» и остались довольны"
            elif not known:
                by_key[item.key] = Ingredient(item.key, TASTE, describe(item), f"понравилось в «{title}»", True,
                                              item=item.model_dump(by_alias=True))
    return [*by_key.values(), *context_of(state)]
