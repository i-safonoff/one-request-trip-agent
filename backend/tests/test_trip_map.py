import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trip_map
from trip_map.schema import Trip
from trip_map.service import normalize
from trip_map.source import FileTripSource

# the module's own host: the real app mounts the same router (see app/main.py)
app = FastAPI()
app.include_router(trip_map.router)
trip_map.mount_static(app)

EXAMPLES = Path(trip_map.__file__).parent / "examples"
FAKE_COORDS = {"токио": (35.68, 139.76), "осака": (34.69, 135.50)}


def fake_geocode(name):
    return FAKE_COORDS.get(name.lower())


def load(name) -> Trip:
    return Trip.model_validate(json.loads((EXAMPLES / name).read_text("utf-8")))


def test_draft_geocodes_named_places_and_skips_nameless():
    view = normalize(load("trip.draft.json"), fake_geocode)
    assert view["places"]["tokyo"]["lat"] == 35.68
    assert view["places"]["origin"]["lat"] is None
    assert [s["id"] for s in view["steps"]] == ["outbound", "stay_tokyo", "tokyo_osaka", "stay_osaka", "return"]
    assert all(s["offer"] is None for s in view["steps"])


def test_filled_plan_resolves_offers_nights_and_local_moves():
    view = normalize(load("trip.json"), fake_geocode)
    steps = {s["id"]: s for s in view["steps"]}
    assert steps["stay_tokyo"]["nights"] == 4
    assert steps["stay_tokyo"]["offer"]["name"] == "Shinjuku Granbell Hotel"
    assert steps["transfer_tokyo"]["local"] is True   # airport nested in the city
    assert steps["tokyo_osaka"]["local"] is False


def test_given_coordinates_skip_the_geocoder():
    def boom(_):
        raise AssertionError("geocoder must not be called")
    normalize(load("trip.json"), boom)


@pytest.mark.parametrize("patch, msg", [
    (lambda d: d["sequence"].append("ghost"), "unknown node"),
    (lambda d: d["nodes"]["stay_tokyo"].update(place="nowhere"), "unknown place"),
    (lambda d: d["nodes"]["stay_tokyo"].update(check_out="2026-11-01"), "check_out"),
])
def test_invalid_graph_is_rejected(patch, msg):
    data = json.loads((EXAMPLES / "trip.json").read_text("utf-8"))
    patch(data)
    with pytest.raises(ValueError, match=msg):
        Trip.model_validate(data)


def test_api_roundtrip(tmp_path):
    src = FileTripSource(tmp_path / "t.json")
    app.dependency_overrides[trip_map.get_source] = lambda: src
    app.dependency_overrides[trip_map.get_geocoder] = lambda: fake_geocode
    try:
        c = TestClient(app)
        assert c.get("/trip-map/api/trip").status_code == 404
        plan = json.loads((EXAMPLES / "trip.draft.json").read_text("utf-8"))
        assert c.put("/trip-map/api/trip", json=plan).status_code == 200
        body = c.get("/trip-map/api/trip").json()
        assert body["trip_id"] == "trip_01" and len(body["steps"]) == 5
        assert c.get("/trip-map/ui/").status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_map_config_defaults_and_env(monkeypatch):
    c = TestClient(app)
    monkeypatch.delenv("TRIP_MAP_LANGUAGE", raising=False)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.delenv("TRIP_MAP_PROVIDER", raising=False)
    monkeypatch.delenv("TRIP_MAP_STYLE_URL", raising=False)
    cfg = c.get("/trip-map/api/config").json()
    assert cfg["provider"] == "maplibre" and cfg["language"] == "ru" and cfg["google_maps_key"] is None
    assert cfg["style_url"].startswith("https://tiles.openfreemap.org/")
    monkeypatch.setenv("TRIP_MAP_LANGUAGE", "en")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "k")
    body = c.get("/trip-map/api/config").json()
    assert body["language"] == "en" and body["google_maps_key"] == "k"


def test_invalid_language_is_rejected(monkeypatch):
    monkeypatch.setenv("TRIP_MAP_LANGUAGE", "ru&key=x")
    with pytest.raises(ValueError, match="TRIP_MAP_LANGUAGE"):
        from trip_map.settings import get_settings
        get_settings()


# ---- road routing ----

from trip_map.routing import OsrmRouter  # noqa: E402

OSRM_OK = json.dumps({"code": "Ok", "routes": [{
    "distance": 84000.0, "duration": 4500.0,
    "geometry": {"coordinates": [[140.39, 35.77], [140.0, 35.7], [139.65, 35.67]]}}]}).encode()


class FakeResponse:
    def __init__(self, body): self._b = body
    def read(self, *a): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_osrm_router_swaps_axes_picks_profile_and_caches(tmp_path, monkeypatch):
    urls = []

    def fake_urlopen(req, timeout):
        urls.append(req.full_url)
        return FakeResponse(OSRM_OK)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    router = OsrmRouter("https://osrm.test/routed-{profile}", tmp_path / "routes.json")
    r = router((35.772, 140.3929), (35.6762, 139.6503), "walk")
    assert urls == ["https://osrm.test/routed-foot/route/v1/driving/140.3929,35.772;139.6503,35.6762"
                    "?overview=simplified&geometries=geojson"]
    assert r["points"][0] == [35.77, 140.39]  # OSRM sends lon,lat; we return lat,lon
    assert r["distance_m"] == 84000.0
    router((35.772, 140.3929), (35.6762, 139.6503), "walk")
    assert len(urls) == 1  # second call served from cache
    # the cache survives a restart
    again = OsrmRouter("https://osrm.test/routed-{profile}", tmp_path / "routes.json")
    assert again((35.772, 140.3929), (35.6762, 139.6503), "walk") == r
    assert len(urls) == 1


def test_osrm_router_does_not_cache_transient_failures(tmp_path, monkeypatch):
    calls = []

    def flaky(req, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("down")
        return FakeResponse(OSRM_OK)

    monkeypatch.setattr("urllib.request.urlopen", flaky)
    router = OsrmRouter("https://osrm.test/{profile}", tmp_path / "r.json")
    assert router((1, 1), (2, 2), "drive") is None
    assert router((1, 1), (2, 2), "drive") is not None


def test_osrm_no_route_is_cached(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout: (calls.append(1), FakeResponse(b'{"code":"NoRoute","routes":[]}'))[1])
    router = OsrmRouter("https://osrm.test/{profile}", tmp_path / "r.json")
    assert router((1, 1), (2, 2), "drive") is None
    assert router((1, 1), (2, 2), "drive") is None
    assert len(calls) == 1


def test_route_endpoint(monkeypatch):
    seen = []

    def fake_router(a, b, mode):
        seen.append((a, b, mode))
        return None if mode == "bike" else {"points": [[1.0, 2.0]], "distance_m": 10.0, "duration_s": 5.0}

    app.dependency_overrides[trip_map.get_router] = lambda: fake_router
    try:
        c = TestClient(app)
        q = {"from_lat": 1, "from_lon": 2, "to_lat": 3, "to_lon": 4}
        assert c.get("/trip-map/api/route", params=q).json()["distance_m"] == 10.0
        assert seen == [((1.0, 2.0), (3.0, 4.0), "drive")]  # default mode
        assert c.get("/trip-map/api/route", params={**q, "mode": "bike"}).status_code == 404
        assert c.get("/trip-map/api/route", params={**q, "mode": "teleport"}).status_code == 422
        assert c.get("/trip-map/api/route", params={**q, "from_lat": 999}).status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_mode_is_passed_through_to_steps():
    steps = {s["id"]: s for s in normalize(load("trip.json"), fake_geocode)["steps"]}
    assert steps["transfer_tokyo"]["mode"] == "drive" and steps["tokyo_osaka"]["mode"] == "rail"
    assert steps["stay_tokyo"]["mode"] is None


def test_invalid_provider_is_rejected(monkeypatch):
    monkeypatch.setenv("TRIP_MAP_PROVIDER", "mapbox")
    with pytest.raises(ValueError, match="TRIP_MAP_PROVIDER"):
        from trip_map.settings import get_settings
        get_settings()
