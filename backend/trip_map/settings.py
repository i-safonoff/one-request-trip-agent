"""Module settings, read from the environment (all optional)."""
import os
import re
from dataclasses import dataclass
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    # file used by the default FileTripSource
    trip_file: Path
    # geocoder cache
    geocache: Path
    # language of map labels (BCP-47 like "ru", "en", "pt-BR"); the page can override it with ?lang=
    language: str
    # map renderer: "maplibre" (default, no key), "google" (needs a key) or "leaflet" (raster OSM)
    provider: str
    # MapLibre style JSON; the default is OpenFreeMap, which is free and needs no key
    style_url: str
    # Maps JavaScript API key. Browser-side by design: restrict it by HTTP referrer in Google Cloud.
    # Only used with provider "google"; without it the frontend falls back to raster OSM (local-language labels).
    google_maps_key: str | None
    # Google "Map ID" (required for advanced markers); DEMO_MAP_ID is Google's placeholder for development
    google_maps_map_id: str
    # OSRM-compatible routing API; {profile} is replaced by car / foot / bike
    routing_url: str
    routing_cache: Path


def get_settings() -> Settings:
    data_dir = Path(os.environ.get("TRIP_MAP_DATA_DIR", "data/trip_map"))
    language = os.environ.get("TRIP_MAP_LANGUAGE", "ru")
    if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,4})?", language):
        raise ValueError(f"TRIP_MAP_LANGUAGE is not a valid language code: {language!r}")
    provider = os.environ.get("TRIP_MAP_PROVIDER", "maplibre")
    if provider not in ("maplibre", "google", "leaflet"):
        raise ValueError(f"TRIP_MAP_PROVIDER must be maplibre, google or leaflet: {provider!r}")
    return Settings(
        trip_file=Path(os.environ.get("TRIP_MAP_FILE", PKG_DIR / "examples" / "trip.json")),
        geocache=data_dir / "geocache.json",
        language=language,
        provider=provider,
        style_url=os.environ.get("TRIP_MAP_STYLE_URL", "https://tiles.openfreemap.org/styles/liberty"),
        google_maps_key=os.environ.get("GOOGLE_MAPS_API_KEY") or None,
        google_maps_map_id=os.environ.get("GOOGLE_MAPS_MAP_ID", "DEMO_MAP_ID"),
        routing_url=os.environ.get(
            "TRIP_MAP_ROUTING_URL", "https://routing.openstreetmap.de/routed-{profile}"),
        routing_cache=data_dir / "routes.json",
    )
