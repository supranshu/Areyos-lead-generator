from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Area:
    """A geocoded city: centre point plus bounding box."""

    lat: float
    lng: float
    south: float
    west: float
    north: float
    east: float
    label: str = ""

    def tiles(self, n: int) -> list["Area"]:
        """Split the box into an n x n grid."""
        n = max(1, n)
        dlat = (self.north - self.south) / n
        dlng = (self.east - self.west) / n
        out = []
        for row in range(n):
            for col in range(n):
                s, w = self.south + row * dlat, self.west + col * dlng
                # Pin the outer edges to the original box so float error cannot leave gaps.
                n_ = self.north if row == n - 1 else s + dlat
                e_ = self.east if col == n - 1 else w + dlng
                out.append(Area((s + n_) / 2, (w + e_) / 2, s, w, n_, e_, f"{self.label} [{row},{col}]"))
        return out

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("lat", "lng", "south", "west", "north", "east", "label")}

    @classmethod
    def from_dict(cls, d: dict) -> "Area":
        return cls(**d)


@dataclass
class RawPlace:
    """A venue as reported by one source, before merging into a Prospect."""

    source: str
    source_id: str
    name: str
    lat: float | None = None
    lng: float | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    website: str | None = None
    phone: str | None = None
    maps_url: str | None = None
    rating: float | None = None
    rating_count: int | None = None
    business_status: str | None = None
    types: list[str] = field(default_factory=list)
    tags: dict = field(default_factory=dict)
    footprint_m2: float | None = None
    raw: dict = field(default_factory=dict)


class DiscoveryProvider(Protocol):
    name: str

    def search(self, industry_key: str, area: Area, query: str | None = None) -> list[RawPlace]: ...
