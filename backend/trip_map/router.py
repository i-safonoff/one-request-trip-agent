from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .geocode import NominatimGeocoder
from .routing import Mode, OsrmRouter, Router
from .schema import Trip
from .service import Geocoder, normalize
from .settings import PKG_DIR, get_settings
from .source import FileTripSource, TripNotFound, TripSource

PREFIX = "/trip-map"
router = APIRouter(prefix=PREFIX, tags=["trip-map"])


@lru_cache
def _default_source() -> FileTripSource:
    return FileTripSource(get_settings().trip_file)


@lru_cache
def _default_geocoder() -> NominatimGeocoder:
    return NominatimGeocoder(get_settings().geocache)


@lru_cache
def _default_router() -> OsrmRouter:
    s = get_settings()
    return OsrmRouter(s.routing_url, s.routing_cache)


# Dependencies: override via app.dependency_overrides to plug in another source / geocoder.
def get_source() -> TripSource:
    return _default_source()


def get_geocoder() -> Geocoder:
    return _default_geocoder()


@router.get("/api/trip")
def get_trip(source: TripSource = Depends(get_source), geocode: Geocoder = Depends(get_geocoder)):
    try:
        trip = source.load()
    except TripNotFound as e:
        raise HTTPException(404, f"Trip not found: {e}")
    except (ValidationError, ValueError) as e:
        raise HTTPException(422, f"Invalid trip: {e}")
    return normalize(trip, geocode)


def get_router() -> Router:
    return _default_router()


@router.get("/api/route")
def get_route(
    from_lat: float = Query(ge=-90, le=90), from_lon: float = Query(ge=-180, le=180),
    to_lat: float = Query(ge=-90, le=90), to_lon: float = Query(ge=-180, le=180),
    mode: Mode = "drive",
    route: Router = Depends(get_router),
):
    """Road route between two points: {points: [[lat, lon], ...], distance_m, duration_s}."""
    result = route((from_lat, from_lon), (to_lat, to_lon), mode)
    if result is None:
        raise HTTPException(404, "No route found (or the routing service is unavailable)")
    return result


@router.get("/api/config")
def get_map_config():
    """Map settings for the frontend (renderer, style, label language, optional Google key)."""
    s = get_settings()
    return {
        "provider": s.provider, "language": s.language, "style_url": s.style_url,
        "google_maps_key": s.google_maps_key, "map_id": s.google_maps_map_id,
    }


@router.put("/api/trip")
def put_trip(trip: Trip, source: TripSource = Depends(get_source)):
    """Replace the stored plan (only for sources that can save)."""
    if not hasattr(source, "save"):
        raise HTTPException(405, "This trip source is read-only")
    source.save(trip)
    return {"ok": True, "nodes": len(trip.sequence)}


def mount_static(app) -> None:
    """Serve the frontend at /trip-map/ui/. Mounts can't live on a router, so the host calls this."""
    app.mount(f"{PREFIX}/ui", StaticFiles(directory=PKG_DIR / "static", html=True), name="trip-map-ui")
