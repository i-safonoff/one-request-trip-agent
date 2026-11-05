"""Wiring: providers, LLM and the orchestrator built from settings."""
from __future__ import annotations

from .runtime.db_store import DbPlanStore
from .runtime.destinations import DestinationAgent
from .runtime.orchestrator import Orchestrator
from .runtime.search import Searcher
from .config import Settings
from .integrations.hotels import HotelProvider, MockHotels, TutuHotels
from .integrations.llm import make_llm
from .integrations.mcp_client import McpClient
from .integrations.transport import MockTransport, TransportProvider, TutuTransport


def make_geocoder():
    from trip_map.geocode import NominatimGeocoder
    from trip_map.settings import get_settings as map_settings
    return NominatimGeocoder(map_settings().geocache)


def make_providers(settings: Settings, geocoder=None) -> tuple[HotelProvider, TransportProvider, McpClient | None]:
    """One MCP session shared by hotels and transport. Sources: auto (live, demo data only as a labelled
    fallback) | tutu (live only) | mock (demo only)."""
    need_mcp = "mock" != settings.hotels_source or "mock" != settings.transport_source
    mcp = McpClient(settings.tutu_mcp_url, timeout=settings.tutu_timeout) if need_mcp else None
    hotels = TutuHotels(mcp) if mcp and settings.hotels_source != "mock" else MockHotels()
    transport = TutuTransport(mcp, geocoder if settings.nominatim_enabled else None) if mcp and settings.transport_source != "mock" else MockTransport()
    return hotels, transport, mcp


def make_orchestrator(settings: Settings, store: DbPlanStore) -> Orchestrator:
    geocoder = make_geocoder()
    hotels, transport, _ = make_providers(settings, geocoder)
    llm = make_llm(settings)
    searcher = Searcher(hotels, transport, settings, geocoder=geocoder)
    return Orchestrator(llm, searcher, DestinationAgent(llm, hotels, settings), store, settings)
