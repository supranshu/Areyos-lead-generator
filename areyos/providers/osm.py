"""OpenStreetMap providers: Overpass for venues, Nominatim for geocoding.

Both are public services with usage policies (identify yourself, at most about
one request per second, no bulk scraping). Data is (c) OpenStreetMap
contributors, ODbL; keep attribution when you show it.
"""

import math

from areyos.catalog.industries import get_industry
from areyos.config import get_settings
from areyos.http import PoliteClient
from areyos.providers.base import Area, RawPlace


class OverpassProvider:
    name = "osm"

    def __init__(self, client: PoliteClient | None = None):
        self.url = get_settings().overpass_url
        self.client = client or PoliteClient()

    @staticmethod
    def build_query(industry_key: str, area: Area, limit: int = 1000) -> str:
        industry = get_industry(industry_key)
        bbox = f"({area.south},{area.west},{area.north},{area.east})"
        selectors = "".join(f"{sel}{bbox};" for sel in industry.osm_selectors)
        # Nodes need "body" to carry coordinates; ways/relations only need tags plus a bounding box.
        return (
            f"[out:json][timeout:90];({selectors})->.all;"
            f"node.all;out body {limit};"
            f"(way.all;rel.all;);out tags bb {limit};"
        )

    def search(self, industry_key: str, area: Area, query: str | None = None) -> list[RawPlace]:
        response = self.client.post(self.url, data={"data": self.build_query(industry_key, area)}, retries=3)
        response.raise_for_status()
        return [p for p in (parse_element(e) for e in response.json().get("elements", [])) if p]


def _bbox_area_m2(bounds: dict | None) -> float | None:
    if not bounds:
        return None
    lat_m = (bounds["maxlat"] - bounds["minlat"]) * 111_320
    mid = math.radians((bounds["maxlat"] + bounds["minlat"]) / 2)
    lng_m = (bounds["maxlon"] - bounds["minlon"]) * 111_320 * math.cos(mid)
    # A bounding box overstates an irregular footprint; scale it down.
    return round(lat_m * lng_m * 0.6, 1)


def parse_element(e: dict) -> RawPlace | None:
    tags = e.get("tags") or {}
    name = tags.get("name:en") or tags.get("name")
    if not name:
        return None
    if e["type"] == "node":
        lat, lng = e.get("lat"), e.get("lon")
    else:
        b = e.get("bounds")
        if not b:
            return None
        lat, lng = (b["minlat"] + b["maxlat"]) / 2, (b["minlon"] + b["maxlon"]) / 2
    street = " ".join(filter(None, [tags.get("addr:housenumber"), tags.get("addr:street")]))
    address = ", ".join(filter(None, [street, tags.get("addr:suburb"), tags.get("addr:city"), tags.get("addr:postcode")]))
    return RawPlace(
        source="osm",
        source_id=f"{e['type']}/{e['id']}",
        name=name,
        lat=lat,
        lng=lng,
        address=address or None,
        city=tags.get("addr:city"),
        state=tags.get("addr:state"),
        postal_code=tags.get("addr:postcode"),
        website=tags.get("website") or tags.get("contact:website"),
        phone=tags.get("phone") or tags.get("contact:phone"),
        maps_url=f"https://www.openstreetmap.org/{e['type']}/{e['id']}",
        types=[f"{k}={v}" for k, v in tags.items() if k in {"amenity", "shop", "tourism", "leisure", "aeroway", "railway", "landuse", "office", "healthcare", "building", "man_made"}],
        tags=tags,
        footprint_m2=_bbox_area_m2(e.get("bounds")) if e["type"] != "node" else None,
        raw=e,
    )


class NominatimGeocoder:
    def __init__(self, client: PoliteClient | None = None):
        self.url = get_settings().nominatim_url.rstrip("/") + "/search"
        # Nominatim policy: max 1 request per second.
        self.client = client or PoliteClient(per_host_delay=1.1)

    def geocode(self, city: str, state: str) -> Area | None:
        response = self.client.get(
            self.url,
            params={"city": city, "state": state, "country": "India", "format": "jsonv2", "limit": 1},
        )
        response.raise_for_status()
        results = response.json()
        if not results:
            return None
        r = results[0]
        south, north, west, east = (float(x) for x in r["boundingbox"])
        return Area(float(r["lat"]), float(r["lon"]), south, west, north, east, f"{city}, {state}")
