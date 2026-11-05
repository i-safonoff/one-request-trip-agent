"""Trip map module: renders a trip plan (hotels, transfers) on a map.

Host usage:
    app.include_router(trip_map.router)
    trip_map.mount_static(app)
"""
from .router import _default_source as router_default_source
from .router import get_geocoder, get_router, get_source, mount_static, router

__all__ = ["router", "mount_static", "get_source", "get_geocoder", "get_router", "router_default_source"]
