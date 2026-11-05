"""Human-readable text for items: what the user is asked to accept, and why."""
from shared.templates import ConstraintItem, HotelHint, PriorityItem

PRIORITY_TEXT = {
    "price": "Подешевле", "location": "Хорошее расположение", "rating": "Высокий рейтинг",
    "cancellation": "Гибкие условия отмены", "meal": "Хорошее питание", "stars": "Класс отеля",
    "duration": "Короче в пути", "transfers": "Меньше пересадок", "schedule": "Удобное время",
}
CONSTRAINT_TEXT = {
    "breakfast": "Завтрак включён", "free_cancellation": "Бесплатная отмена", "pets_allowed": "Можно с животными",
    "kid_friendly": "Подходит для детей", "avoid_overnight": "Без ночных рейсов",
}


MODE_GEN = {"bus": "автобусов", "flight": "перелётов", "rail": "поездов", "drive": "машины", "ferry": "паромов"}
MODE_ONLY = {"bus": "автобусы", "flight": "самолёты", "rail": "поезда", "drive": "машина", "ferry": "паромы"}


def describe(item: ConstraintItem | PriorityItem | HotelHint) -> str:
    if isinstance(item, HotelHint):
        return f"Вы уже были в «{item.name}» и остались довольны" if item.polarity == "like" \
            else f"Не предлагать «{item.name}»"
    if isinstance(item, PriorityItem):
        return PRIORITY_TEXT[item.key_]
    if item.kind == "min_stars":
        return f"Отель от {int(item.value)}★"
    if item.kind == "min_rating":
        return f"Рейтинг от {item.value:g}"
    if item.kind == "max_travel_hours":
        return f"В пути не дольше {item.value:g} ч"
    if item.kind == "max_transfers":
        return f"Не больше {int(item.value)} пересадок"
    if item.kind in ("exclude_modes", "only_modes"):
        if item.kind == "exclude_modes":
            return "Без " + ", ".join(MODE_GEN.get(m, m) for m in item.value)
        return "Только " + ", ".join(MODE_ONLY.get(m, m) for m in item.value)
    return CONSTRAINT_TEXT.get(item.kind, item.kind)


def reason(trips: list[str], observed: int) -> str:
    """Why this is being offered, in terms the user can check against their own memory."""
    if observed <= 1 and trips:
        return f"В поездке «{trips[0]}» вам это понравилось"
    return f"Повторяется в ваших прошлых поездках ({observed})"
