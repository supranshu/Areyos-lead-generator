"""Google Places API (New) Text Search provider.

Docs: https://developers.google.com/maps/documentation/places/web-service/text-search
"""

from areyos.config import get_settings
from areyos.http import PoliteClient
from areyos.providers.base import Area, RawPlace

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

BASE_FIELDS = [
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.addressComponents",
    "places.location",
    "places.viewport",
    "places.types",
    "places.primaryType",
    "places.businessStatus",
    "places.googleMapsUri",
]
CONTACT_FIELDS = [
    "places.websiteUri",
    "places.nationalPhoneNumber",
    "places.internationalPhoneNumber",
    "places.rating",
    "places.userRatingCount",
]


class GooglePlacesError(Exception):
    pass


class GooglePlacesProvider:
    name = "google_places"

    def __init__(self, api_key: str | None = None, client: PoliteClient | None = None):
        settings = get_settings()
        self.api_key = api_key or settings.google_places_api_key
        if not self.api_key:
            raise GooglePlacesError("AREYOS_GOOGLE_PLACES_API_KEY is not set")
        self.max_pages = settings.google_max_pages
        fields = BASE_FIELDS + (CONTACT_FIELDS if settings.google_contact_fields else [])
        self.field_mask = ",".join(fields + ["nextPageToken"])
        # Google's quota is per project, not per host politeness; keep a small gap anyway.
        self.client = client or PoliteClient(per_host_delay=0.2)

    def _post(self, body: dict, field_mask: str) -> dict:
        response = self.client.post(
            SEARCH_URL,
            json=body,
            headers={"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": field_mask},
        )
        if response.status_code == 403:
            raise GooglePlacesError(f"Google Places refused the request: {response.text[:300]}")
        if response.status_code >= 400:
            raise GooglePlacesError(f"Google Places HTTP {response.status_code}: {response.text[:300]}")
        return response.json()

    def search(self, industry_key: str, area: Area, query: str | None = None) -> list[RawPlace]:
        if not query:
            raise ValueError("Google text search needs a query")
        body = {
            "textQuery": query,
            "pageSize": 20,
            "regionCode": "IN",
            "languageCode": "en",
            "locationRestriction": {
                "rectangle": {
                    "low": {"latitude": area.south, "longitude": area.west},
                    "high": {"latitude": area.north, "longitude": area.east},
                }
            },
        }
        places: list[RawPlace] = []
        for _ in range(self.max_pages):
            data = self._post(body, self.field_mask)
            places.extend(parse_place(p) for p in data.get("places", []))
            token = data.get("nextPageToken")
            if not token:
                break
            body = {**body, "pageToken": token}
        return places

    def geocode(self, city: str, state: str) -> Area | None:
        data = self._post(
            {"textQuery": f"{city}, {state}, India", "regionCode": "IN", "pageSize": 1},
            "places.displayName,places.location,places.viewport",
        )
        results = data.get("places") or []
        if not results:
            return None
        place = results[0]
        loc, vp = place.get("location"), place.get("viewport")
        if not loc or not vp:
            return None
        return Area(
            lat=loc["latitude"],
            lng=loc["longitude"],
            south=vp["low"]["latitude"],
            west=vp["low"]["longitude"],
            north=vp["high"]["latitude"],
            east=vp["high"]["longitude"],
            label=f"{city}, {state}",
        )


def _component(components: list[dict], kind: str) -> str | None:
    for c in components:
        if kind in c.get("types", []):
            return c.get("longText")
    return None


def parse_place(p: dict) -> RawPlace:
    components = p.get("addressComponents", [])
    loc = p.get("location") or {}
    types = list(p.get("types", []))
    if p.get("primaryType") and p["primaryType"] not in types:
        types.insert(0, p["primaryType"])
    return RawPlace(
        source="google_places",
        source_id=p["id"],
        name=(p.get("displayName") or {}).get("text") or "",
        lat=loc.get("latitude"),
        lng=loc.get("longitude"),
        address=p.get("formattedAddress"),
        city=_component(components, "locality") or _component(components, "administrative_area_level_3"),
        state=_component(components, "administrative_area_level_1"),
        postal_code=_component(components, "postal_code"),
        website=p.get("websiteUri"),
        phone=p.get("internationalPhoneNumber") or p.get("nationalPhoneNumber"),
        maps_url=p.get("googleMapsUri"),
        rating=p.get("rating"),
        rating_count=p.get("userRatingCount"),
        business_status=p.get("businessStatus"),
        types=types,
        raw=p,
    )

