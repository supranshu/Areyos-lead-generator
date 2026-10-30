"""Per-industry discovery and scoring configuration.

Each industry says how to find venues (Google text queries, OSM tag selectors),
how to recognise a genuine match, how big "big" is for that kind of venue, and
how well indoor wayfinding fits it (``fit_weight``, 0-30).
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SizeThresholds:
    """Lower bounds for medium / large / mega."""

    medium: float
    large: float
    mega: float

    def classify(self, value: float | None) -> str | None:
        if value is None:
            return None
        if value >= self.mega:
            return "mega"
        if value >= self.large:
            return "large"
        if value >= self.medium:
            return "medium"
        return "small"


@dataclass(frozen=True)
class Industry:
    key: str
    label: str
    fit_weight: int
    google_queries: tuple[str, ...]
    google_types: frozenset[str]
    osm_selectors: tuple[str, ...]
    # Google review count is the footfall proxy available for every place.
    review_thresholds: SizeThresholds
    # Optional OSM tag (beds, rooms, capacity) that measures size directly.
    capacity_tag: str | None = None
    capacity_thresholds: SizeThresholds | None = None
    # Name fragments that indicate at least a large venue.
    large_keywords: tuple[str, ...] = field(default_factory=tuple)
    # Name fragments that confirm the place belongs to this industry.
    name_keywords: tuple[str, ...] = field(default_factory=tuple)


# Rough footprint (m^2) thresholds from an OSM element's bounding box; shared by all industries.
FOOTPRINT_THRESHOLDS = SizeThresholds(medium=5_000, large=20_000, mega=80_000)

INDUSTRIES: dict[str, Industry] = {
    i.key: i
    for i in [
        Industry(
            key="healthcare",
            label="Hospitals & healthcare facilities",
            fit_weight=30,
            google_queries=("multi speciality hospital", "hospital", "medical college hospital"),
            google_types=frozenset({"hospital", "general_hospital", "medical_center"}),
            osm_selectors=('nwr["amenity"="hospital"]["name"]', 'nwr["healthcare"="hospital"]["name"]'),
            review_thresholds=SizeThresholds(300, 1_500, 5_000),
            capacity_tag="beds",
            capacity_thresholds=SizeThresholds(100, 300, 750),
            large_keywords=("multi speciality", "multispeciality", "super speciality", "medical college", "aiims", "institute of medical"),
            name_keywords=("hospital", "medical", "health", "clinic", "care", "institute"),
        ),
        Industry(
            key="retail",
            label="Shopping malls & retail complexes",
            fit_weight=28,
            google_queries=("shopping mall", "retail complex", "hypermarket"),
            google_types=frozenset({"shopping_mall", "department_store", "hypermarket"}),
            osm_selectors=('nwr["shop"="mall"]["name"]', 'nwr["shop"="department_store"]["name"]'),
            review_thresholds=SizeThresholds(1_000, 8_000, 30_000),
            large_keywords=("phoenix", "palladium", "lulu", "nexus", "inorbit"),
            name_keywords=("mall", "plaza", "square", "market", "centre", "center", "city"),
        ),
        Industry(
            key="convention",
            label="Convention & exhibition centres",
            fit_weight=28,
            google_queries=("convention centre", "exhibition centre", "trade fair ground"),
            google_types=frozenset({"convention_center", "event_venue"}),
            osm_selectors=('nwr["amenity"="conference_centre"]["name"]', 'nwr["amenity"="exhibition_centre"]["name"]'),
            review_thresholds=SizeThresholds(200, 1_000, 5_000),
            large_keywords=("international", "expo", "exhibition centre", "trade centre"),
            name_keywords=("convention", "exhibition", "expo", "trade", "conference"),
        ),
        Industry(
            key="education",
            label="Universities & educational campuses",
            fit_weight=25,
            google_queries=("university", "college campus", "institute of technology"),
            google_types=frozenset({"university", "school", "secondary_school"}),
            osm_selectors=('nwr["amenity"="university"]["name"]', 'nwr["amenity"="college"]["name"]'),
            review_thresholds=SizeThresholds(200, 1_000, 4_000),
            large_keywords=("university", "iit ", "indian institute", "campus"),
            name_keywords=("university", "college", "institute", "school", "academy", "vidyalaya", "vidyapeeth"),
        ),
        Industry(
            key="transport",
            label="Airports & railway stations",
            fit_weight=30,
            google_queries=("airport", "railway station", "railway junction"),
            google_types=frozenset({"airport", "international_airport", "train_station", "transit_station"}),
            osm_selectors=('nwr["aeroway"="aerodrome"]["iata"]', 'nwr["railway"="station"]["name"]["station"!="subway"]'),
            review_thresholds=SizeThresholds(1_000, 10_000, 40_000),
            large_keywords=("international airport", "junction", "terminus", "central"),
            name_keywords=("airport", "railway", "station", "junction", "terminus", "terminal"),
        ),
        Industry(
            key="corporate",
            label="IT parks & corporate offices",
            fit_weight=22,
            google_queries=("IT park", "tech park", "SEZ", "business park"),
            google_types=frozenset({"corporate_office", "business_center", "coworking_space"}),
            osm_selectors=(
                'nwr["landuse"="commercial"]["name"~"tech|it park|sez|business park|cyber|infotech",i]',
                'nwr["building"="office"]["name"~"tech|it park|sez|business park|cyber|infotech",i]',
            ),
            review_thresholds=SizeThresholds(100, 500, 2_000),
            large_keywords=("sez", "tech park", "it park", "cyber city", "business park", "infopark", "technopark"),
            name_keywords=("tech", "park", "sez", "cyber", "business", "corporate", "info", "tower"),
        ),
        Industry(
            key="hospitality",
            label="Hotels & resorts",
            fit_weight=15,
            google_queries=("5 star hotel", "luxury hotel", "resort"),
            google_types=frozenset({"hotel", "resort_hotel", "lodging"}),
            osm_selectors=('nwr["tourism"="hotel"]["name"]', 'nwr["leisure"="resort"]["name"]'),
            review_thresholds=SizeThresholds(300, 1_500, 5_000),
            capacity_tag="rooms",
            capacity_thresholds=SizeThresholds(50, 150, 400),
            large_keywords=("taj", "oberoi", "marriott", "hyatt", "itc ", "leela", "jw ", "westin", "resort & spa"),
            name_keywords=("hotel", "resort", "inn", "suites", "palace", "retreat"),
        ),
        Industry(
            key="government",
            label="Government buildings",
            fit_weight=18,
            google_queries=("municipal corporation office", "collector office", "government office complex", "district court"),
            google_types=frozenset({"city_hall", "local_government_office", "courthouse", "government_office"}),
            osm_selectors=(
                'nwr["amenity"="townhall"]["name"]',
                'nwr["office"="government"]["name"]',
                'nwr["amenity"="courthouse"]["name"]',
            ),
            review_thresholds=SizeThresholds(50, 300, 1_500),
            large_keywords=("secretariat", "mantralaya", "high court", "municipal corporation", "bhavan"),
            name_keywords=("office", "corporation", "court", "bhavan", "collector", "government", "municipal", "secretariat"),
        ),
        Industry(
            key="event_venues",
            label="Large event venues",
            fit_weight=20,
            google_queries=("event venue", "banquet hall", "auditorium"),
            google_types=frozenset({"event_venue", "banquet_hall", "wedding_venue", "auditorium"}),
            osm_selectors=('nwr["amenity"="events_venue"]["name"]',),
            review_thresholds=SizeThresholds(200, 1_000, 4_000),
            capacity_tag="capacity",
            capacity_thresholds=SizeThresholds(500, 2_000, 10_000),
            large_keywords=("grounds", "lawns", "auditorium", "arena"),
            name_keywords=("hall", "venue", "banquet", "auditorium", "lawns", "grounds", "convention"),
        ),
        Industry(
            key="sports",
            label="Stadiums & indoor sports arenas",
            fit_weight=22,
            google_queries=("stadium", "indoor sports arena", "sports complex"),
            google_types=frozenset({"stadium", "arena", "sports_complex", "athletic_field"}),
            osm_selectors=('nwr["leisure"="stadium"]["name"]', 'nwr["leisure"="sports_centre"]["name"]', 'nwr["leisure"="sports_hall"]["name"]'),
            review_thresholds=SizeThresholds(500, 3_000, 15_000),
            capacity_tag="capacity",
            capacity_thresholds=SizeThresholds(2_000, 10_000, 40_000),
            large_keywords=("stadium", "international", "arena"),
            name_keywords=("stadium", "arena", "sports", "complex", "ground", "gymkhana"),
        ),
        Industry(
            key="cultural",
            label="Museums & cultural centres",
            fit_weight=18,
            google_queries=("museum", "cultural centre", "science centre", "art gallery"),
            google_types=frozenset({"museum", "art_gallery", "cultural_center", "performing_arts_theater", "planetarium"}),
            osm_selectors=('nwr["tourism"="museum"]["name"]', 'nwr["amenity"="arts_centre"]["name"]', 'nwr["amenity"="planetarium"]["name"]'),
            review_thresholds=SizeThresholds(300, 2_000, 8_000),
            large_keywords=("national", "museum", "science city", "kala"),
            name_keywords=("museum", "gallery", "cultural", "science", "kala", "planetarium", "art", "heritage"),
        ),
        Industry(
            key="industrial",
            label="Industrial campuses",
            fit_weight=15,
            google_queries=("industrial park", "industrial estate", "manufacturing plant"),
            google_types=frozenset({"manufacturer", "industrial_area", "factory"}),
            osm_selectors=('nwr["landuse"="industrial"]["name"]', 'nwr["man_made"="works"]["name"]'),
            review_thresholds=SizeThresholds(30, 150, 600),
            large_keywords=("industrial area", "industrial estate", "midc", "sipcot", "gidc", "industrial park"),
            name_keywords=("industrial", "industries", "estate", "plant", "works", "manufactur", "midc"),
        ),
    ]
}


def get_industry(key: str) -> Industry:
    try:
        return INDUSTRIES[key]
    except KeyError:
        raise ValueError(f"Unknown industry {key!r}; expected one of {sorted(INDUSTRIES)}") from None
