"""Place-name -> coordinates, used only for places that come without lat/lon."""
import json
import urllib.parse
import urllib.request
from pathlib import Path

Coords = tuple[float, float]


class NominatimGeocoder:
    """OSM Nominatim with a JSON file cache (its usage policy asks for caching)."""

    def __init__(self, cache_path: Path, timeout: float = 5.0):
        self._path = cache_path
        self._timeout = timeout
        self._cache: dict[str, list[float] | None] | None = None

    def _load(self) -> dict:
        if self._cache is None:
            try:
                self._cache = json.loads(self._path.read_text("utf-8"))
            except (OSError, ValueError):
                self._cache = {}
        return self._cache

    def __call__(self, name: str) -> Coords | None:
        cache, key = self._load(), name.strip().lower()
        if key in cache:
            return tuple(cache[key]) if cache[key] else None
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": name, "format": "json", "limit": 1})
        req = urllib.request.Request(url, headers={"User-Agent": "ekvivalentno-trip-map/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                hits = json.load(r)
        except Exception:
            return None  # deliberately not cached: the network may come back
        cache[key] = [float(hits[0]["lat"]), float(hits[0]["lon"])] if hits else None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
        except OSError:
            pass
        return tuple(cache[key]) if cache[key] else None
