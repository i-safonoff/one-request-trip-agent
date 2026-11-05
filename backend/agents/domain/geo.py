"""Geography helpers: distances, catalog-based place resolution, time-zone estimation."""
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from ..data.cities import CITIES, find_city
from .models import Place


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_catalog_city(lat: float, lon: float) -> tuple[dict, float]:
    best = min(CITIES.values(), key=lambda c: haversine_km(lat, lon, c["lat"], c["lon"]))
    return best, haversine_km(lat, lon, best["lat"], best["lon"])


def estimate_tz(lat: float, lon: float) -> tuple[str, bool]:
    """(tz, estimated). Exact for catalog cities, otherwise the nearest catalog city's zone."""
    city, dist = nearest_catalog_city(lat, lon)
    return city["tz"], dist > 25


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", _translit(name.lower())).strip("-")
    return s or "place"


_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя", ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m",
            "n", "o", "p", "r", "s", "t", "u", "f", "h", "c", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def _translit(s: str) -> str:
    return "".join(_TR.get(ch, ch) for ch in s)


def place_from_name(name: str) -> tuple[str, Place]:
    """Resolve a place name against the catalog; unknown names get a slug id and no coordinates
    (the orchestrator geocodes them later)."""
    c = find_city(name)
    if c:
        return c["id"], Place(name=c["name"], lat=c["lat"], lon=c["lon"], tz=c["tz"], country=c["country"])
    return slug(name), Place(name=name.strip())


def to_local(dt: datetime, tz: str | None) -> datetime:
    return dt.astimezone(ZoneInfo(tz)) if tz else dt
