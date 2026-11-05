"""Road routing through an OSRM-compatible HTTP API.

Defaults to the public FOSSGIS instances (car / foot / bike). They are free but meant for
light use with no SLA, so results are cached by endpoints+mode; for real load point
TRIP_MAP_ROUTING_URL at a self-hosted OSRM.
"""
import json
import urllib.request
from pathlib import Path
from typing import Callable, Literal, TypedDict

from .geocode import Coords

Mode = Literal["drive", "walk", "bike"]
PROFILE = {"drive": "car", "walk": "foot", "bike": "bike"}


class Route(TypedDict):
    points: list[list[float]]  # [[lat, lon], ...]
    distance_m: float
    duration_s: float


Router = Callable[[Coords, Coords, Mode], Route | None]


class OsrmRouter:
    def __init__(self, url_template: str, cache_path: Path, timeout: float = 8.0):
        self._url = url_template  # may contain {profile}
        self._path = cache_path
        self._timeout = timeout
        self._cache: dict[str, Route | None] | None = None

    def _load(self) -> dict:
        if self._cache is None:
            try:
                self._cache = json.loads(self._path.read_text("utf-8"))
            except (OSError, ValueError):
                self._cache = {}
        return self._cache

    def __call__(self, a: Coords, b: Coords, mode: Mode) -> Route | None:
        cache = self._load()
        key = f"{mode}:{a[0]:.5f},{a[1]:.5f}>{b[0]:.5f},{b[1]:.5f}"
        if key in cache:
            return cache[key]
        base = self._url.format(profile=PROFILE[mode])
        url = (f"{base}/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}"
               "?overview=simplified&geometries=geojson")
        req = urllib.request.Request(url, headers={"User-Agent": "ekvivalentno-trip-map/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                data = json.load(r)
        except Exception:
            return None  # transient: don't cache
        if data.get("code") == "NoRoute" or not data.get("routes"):
            route = None  # definitive answer, cache it
        elif data.get("code") != "Ok":
            return None
        else:
            r0 = data["routes"][0]
            route = {
                "points": [[lat, lon] for lon, lat in r0["geometry"]["coordinates"]],
                "distance_m": r0["distance"],
                "duration_s": r0["duration"],
            }
        cache[key] = route
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(cache), "utf-8")
        except OSError:
            pass
        return route
