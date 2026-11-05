"""Climate for the destination agent: Open-Meteo (free, no key). Monthly means from last year's archive."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger("weather")
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
MARINE = "https://marine-api.open-meteo.com/v1/marine"


@dataclass
class Climate:
    air_c: float | None
    sea_c: float | None
    rain_mm_per_day: float | None
    year: int


def _mean(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


async def month_climate(client: httpx.AsyncClient, lat: float, lon: float, sea: tuple[float, float] | None,
                        year: int, month: int) -> Climate | None:
    """Mean air temperature, rain and (if `sea` is given) sea-surface temperature for `month` of `year`."""
    import calendar
    last = calendar.monthrange(year, month)[1]
    start, end = f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last:02d}"
    try:
        r = await client.get(ARCHIVE, params={"latitude": lat, "longitude": lon, "start_date": start, "end_date": end,
                                              "daily": "temperature_2m_mean,precipitation_sum", "timezone": "UTC"})
        r.raise_for_status()
        daily = r.json()["daily"]
        air, rain = _mean(daily["temperature_2m_mean"]), _mean(daily["precipitation_sum"])
        sea_c = None
        if sea:
            m = await client.get(MARINE, params={"latitude": sea[0], "longitude": sea[1], "start_date": start,
                                                 "end_date": end, "hourly": "sea_surface_temperature", "timezone": "UTC"})
            m.raise_for_status()
            sea_c = _mean(m.json().get("hourly", {}).get("sea_surface_temperature") or [])
        return Climate(air, sea_c, rain, year)
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("climate lookup failed: %s", e)
        return None
