# trip_map

The module that shows a trip plan on a map: cities, hotels, legs between them. Part of the single FastAPI backend.

## Wiring it into the host

```python
import trip_map

app.include_router(trip_map.router)   # API: /trip-map/api/trip
trip_map.mount_static(app)            # UI:  /trip-map/ui/
```

The plan source is swapped through FastAPI dependencies (for example, the agents module serves the plan from its own store):

```python
app.dependency_overrides[trip_map.get_source] = lambda: MySource()   # an object with load() -> trip_map.schema.Trip
```

By default the plan is read from a file (`TRIP_MAP_FILE`, `examples/trip.json` by default).

## Plan format

A graph: `places` + `nodes` + `sequence`; the data of the chosen options lives in the top-level `offers` and is resolved through `selected_offer_ref`. Schema: [schema.py](schema.py), examples: [examples/](examples/).

- `places`: any place (a city, an airport, a district) in any country. `lat`/`lon` are optional: without them the geocoder looks the place up by `name`. `parent` nests one place inside another.
- `nodes[*].type`: any string. A node with `place` is drawn as a pin, one with `from`/`to` as a line. A move within one city (`from == to` or nested places) appears only in the list.
- `nodes[*].mode`: how the leg is travelled. `drive` / `walk` / `bike` follow real roads; everything else (`flight`, `rail`, or no mode) is drawn as a straight line (a great-circle arc). A road leg within a city (airport → centre) is drawn as well.
- While `selected_offer_ref` is `null`, the step is shown as "no option chosen".

## Map and label language

The default is **MapLibre GL with OpenFreeMap vector tiles**: free, no key, no sign-up. Labels come from the tiles' `name:<language>` (Russian by default); where there is no name in that language the Latin transliteration is shown, and only then the local name. The language of a single view is set with `/trip-map/ui/?lang=en`.

| Variable | Purpose |
|---|---|
| `TRIP_MAP_PROVIDER` | `maplibre` (default), `google` or `leaflet` (raster OSM, labels in the local language) |
| `TRIP_MAP_LANGUAGE` | label language: `ru`, `en`, `pt-BR`, ... |
| `TRIP_MAP_STYLE_URL` | the MapLibre style, `https://tiles.openfreemap.org/styles/liberty` by default. Any style on the OpenMapTiles schema works (your own server, MapTiler and so on) |
| `GOOGLE_MAPS_API_KEY`, `GOOGLE_MAPS_MAP_ID` | only for `google`. The key goes to the browser, so restrict it by HTTP referrer. `GOOGLE_MAPS_MAP_ID` defaults to `DEMO_MAP_ID` |

If the chosen map fails to load (no WebGL, the tile service is down, the Google key is rejected), the front end falls back to Leaflet with OSM and says so. All providers sit behind one interface in [static/map-adapters.js](static/map-adapters.js); a new provider is one more adapter.

Public OpenFreeMap is free and comes with no guarantees. In production you can host your own tiles and point `TRIP_MAP_STYLE_URL` at their style.

## Routes along roads

The backend builds the geometry (`GET /trip-map/api/route`) through an OSRM-compatible API and caches it in a file, so repeated views never call the external service. The public FOSSGIS servers are used by default (`routing.openstreetmap.de`, car / foot / bike profiles). They are free but come with no guarantees and are meant for light load; in production run your own OSRM and set it in `TRIP_MAP_ROUTING_URL` (a template where `{profile}` becomes `car` / `foot` / `bike`; your own server usually serves one profile, in which case `{profile}` is not needed). If no route comes back, the front end draws a straight line. Railway routes (the Shinkansen) are not built this way: they would need timetable data.

| Variable | Purpose |
|---|---|
| `TRIP_MAP_ROUTING_URL` | the OSRM base URL, `https://routing.openstreetmap.de/routed-{profile}` by default |
| `TRIP_MAP_DATA_DIR` | the directory for the geocoder and route caches, `data/trip_map` by default |

## API

- `GET /trip-map/api/trip`: the normalised plan (`places` with coordinates, `steps` with the offer resolved).
- `GET /trip-map/api/route?from_lat&from_lon&to_lat&to_lon&mode=drive|walk|bike`: a road route `{points, distance_m, duration_s}`, 404 if there is none.
- `GET /trip-map/api/config`: map settings for the front end (provider, style, language, Google key).
- `PUT /trip-map/api/trip`: replace the plan (only if the source supports `save`).

## Tests

```bash
cd backend && pip install -r requirements-dev.txt && pytest
```
