"""Russian number / money / date formatting shared by the view builder and the answer templates."""
from datetime import date, datetime
from decimal import Decimal

_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
           "октября", "ноября", "декабря"]
NBSP = " "


def rub(amount: Decimal | float | int | str | None, currency: str = "RUB") -> str:
    if amount is None:
        return "—"
    n = Decimal(str(amount)).quantize(Decimal("1"))
    sign = {"RUB": "₽", "USD": "$", "EUR": "€"}.get(currency, currency)
    return f"{n:,}".replace(",", NBSP) + NBSP + sign


def fix_money(text: str) -> str:
    """"3315.86 ₽" (a raw number the model copied from the facts) -> "3 316 ₽", the format used everywhere else."""
    import re
    return re.sub(r"(?<![\d.])(\d{1,3}(?:[ \u00a0]\d{3})*|\d+)\.(\d{1,2})(?=\s?₽)",
                  lambda m: f"{Decimal(m.group(1).replace(' ', '').replace(chr(160), '') + '.' + m.group(2)).quantize(Decimal('1')):,}".replace(",", NBSP),
                  text)


def num(x: float | Decimal, digits: int = 1) -> str:
    return f"{x:.{digits}f}".replace(".", ",").rstrip("0").rstrip(",") if digits else str(round(x))


def day(d: date | datetime | None) -> str:
    return "—" if d is None else f"{d.day} {_MONTHS[d.month - 1]}"


def day_range(a: date | None, b: date | None) -> str:
    if not a:
        return "даты не указаны"
    if not b:
        return day(a)
    return f"{a.day}–{b.day} {_MONTHS[b.month - 1]}" if a.month == b.month else f"{day(a)} – {day(b)}"


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    word = one if n % 10 == 1 and n % 100 != 11 else few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many
    return f"{n} {word}"


def duration(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h} ч {m:02d} мин" if h and m else f"{h} ч" if h else f"{m} мин"


def day_iso(iso: str) -> str:
    return day(date.fromisoformat(str(iso)[:10]))
