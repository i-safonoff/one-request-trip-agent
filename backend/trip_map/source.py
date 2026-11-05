"""Where the trip plan comes from.

The map module only needs `load()`. In the merged backend the agent module can
provide its own implementation (DB, in-memory per-session store, ...) and plug it in
with `app.dependency_overrides[trip_map.get_source]`.
"""
import json
from pathlib import Path
from typing import Protocol

from .schema import Trip


class TripNotFound(Exception):
    pass


class TripSource(Protocol):
    def load(self) -> Trip: ...


class FileTripSource:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Trip:
        if not self.path.exists():
            raise TripNotFound(str(self.path))
        return Trip.model_validate(json.loads(self.path.read_text("utf-8")))

    def save(self, trip: Trip) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(trip.model_dump_json(indent=2, by_alias=True), "utf-8")
