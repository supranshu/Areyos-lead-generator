import httpx
import respx

from areyos.catalog.industries import get_industry
from areyos.dedup import name_similarity, normalize_name
from areyos.enrichment.website import WebsiteEnricher, contact_links, extract_contacts
from areyos.providers.base import Area
from areyos.providers.google_places import SEARCH_URL, GooglePlacesProvider
from areyos.providers.osm import OverpassProvider, parse_element
from areyos.scoring import classify_size, score

MUMBAI = Area(19.07, 72.87, 18.89, 72.77, 19.27, 72.98, "Mumbai")


def test_tiles_cover_area():
    tiles = MUMBAI.tiles(2)
    assert len(tiles) == 4
    assert min(t.south for t in tiles) == MUMBAI.south and max(t.north for t in tiles) == MUMBAI.north
    assert min(t.west for t in tiles) == MUMBAI.west and max(t.east for t in tiles) == MUMBAI.east


def test_size_uses_strongest_signal():
    hospital = get_industry("healthcare")
    assert classify_size(hospital, "City Clinic", 120, {}) == ("small", {"reviews": "small"})
    size, signals = classify_size(hospital, "City Clinic", 120, {"beds": "450"})
    assert size == "large" and signals["beds"] == "large"
    assert classify_size(hospital, "Kokilaben Multi Speciality Hospital", None, {})[0] == "large"
    assert classify_size(hospital, "Somewhere", None, {})[0] == "unknown"
    assert classify_size(get_industry("transport"), "X", None, {"iata": "BOM"})[0] == "large"


def test_priority_score():
    total, level, b = score(
        industry_key="healthcare", name="Lilavati Hospital", size_class="mega", rating_count=20000,
        types=["hospital"], website="https://x", phone="1", has_email=True, business_status="OPERATIONAL",
    )
    assert total == 100 and level == "high" and b["industry_match"]

    total, level, b = score(
        industry_key="hospitality", name="Random Shop", size_class="small", rating_count=3,
        types=["store"], website=None, phone=None, has_email=False, business_status="OPERATIONAL",
    )
    assert level == "low" and b["industry_fit"] == 7  # mismatched type halves fit

    total, level, _ = score(
        industry_key="retail", name="Old Mall", size_class="mega", rating_count=50000,
        types=["shopping_mall"], website="w", phone="p", has_email=True, business_status="CLOSED_PERMANENTLY",
    )
    assert total == 0 and level == "low"


def test_name_matching():
    assert normalize_name("The Phoenix Mills Pvt. Ltd.") == "phoenix mills"
    assert normalize_name("Bombay Exhibition Centre") == normalize_name("Bombay Exhibition Center")
    assert name_similarity("phoenix palladium", "phoenix palladium mall") == 1.0
    assert name_similarity("lilavati hospital", "nanavati hospital") < 0.6


@respx.mock
def test_google_search_paginates_and_parses():
    page1 = {
        "places": [{
            "id": "g1", "displayName": {"text": "Lilavati Hospital"}, "formattedAddress": "Bandra West, Mumbai",
            "location": {"latitude": 19.05, "longitude": 72.83}, "types": ["hospital"], "primaryType": "hospital",
            "addressComponents": [
                {"longText": "Mumbai", "types": ["locality"]},
                {"longText": "Maharashtra", "types": ["administrative_area_level_1"]},
                {"longText": "400050", "types": ["postal_code"]},
            ],
            "websiteUri": "https://lilavatihospital.com", "internationalPhoneNumber": "+91 22 6931 8000",
            "userRatingCount": 9000, "rating": 4.2, "businessStatus": "OPERATIONAL",
        }],
        "nextPageToken": "tok",
    }
    page2 = {"places": [{"id": "g2", "displayName": {"text": "Second"}, "location": {"latitude": 19.1, "longitude": 72.9}}]}
    route = respx.post(SEARCH_URL).mock(side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)])

    places = GooglePlacesProvider(api_key="k").search("healthcare", MUMBAI, "hospital")

    assert [p.source_id for p in places] == ["g1", "g2"]
    first = places[0]
    assert (first.city, first.state, first.postal_code, first.rating_count) == ("Mumbai", "Maharashtra", "400050", 9000)
    sent = route.calls[0].request
    assert sent.headers["X-Goog-Api-Key"] == "k"
    assert "places.websiteUri" in sent.headers["X-Goog-FieldMask"]
    assert b'"pageToken":"tok"' in route.calls[1].request.content.replace(b" ", b"")


def test_overpass_query_and_parse():
    q = OverpassProvider.build_query("sports", MUMBAI)
    assert 'nwr["leisure"="stadium"]["name"](18.89,72.77,19.27,72.98);' in q
    assert "node.all;out body" in q and "out tags bb" in q

    way = parse_element({
        "type": "way", "id": 42,
        "bounds": {"minlat": 18.938, "minlon": 72.824, "maxlat": 18.940, "maxlon": 72.827},
        "tags": {"name": "Wankhede Stadium", "leisure": "stadium", "capacity": "33,108", "website": "https://w.example"},
    })
    assert way.source_id == "way/42" and way.footprint_m2 > 30_000
    assert round(way.lat, 3) == 18.939 and "leisure=stadium" in way.types
    assert parse_element({"type": "node", "id": 1, "lat": 1, "lon": 2, "tags": {}}) is None


HTML = """
<html><body>
<a href="/contact-us">Contact Us</a> <a href="https://other.com/contact">elsewhere</a> <a href="/about">About</a>
<p>Write to <a href="mailto:info@venue.in">info@venue.in</a> or marketing@venue.in. Icon logo@2x.png</p>
<p>Call +91 22 2345 6789 or <a href="tel:+919876543210">us</a></p>
<a href="https://www.linkedin.com/company/venue-group/">LinkedIn</a>
</body></html>
"""


def test_extract_contacts():
    found = {(c.kind, c.value) for c in extract_contacts(HTML, "https://venue.in/")}
    assert ("email", "info@venue.in") in found and ("email", "marketing@venue.in") in found
    assert not any(v.endswith(".png") for _, v in found)
    assert ("phone", "+91 2223456789") in found and ("phone", "+91 9876543210") in found
    assert ("linkedin", "https://www.linkedin.com/company/venue-group") in found
    assert contact_links(HTML, "https://venue.in/") == ["https://venue.in/contact-us", "https://venue.in/about"]


@respx.mock
def test_enricher_respects_robots():
    respx.get("https://venue.in/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /contact-us\n"))
    respx.get("https://venue.in/").mock(return_value=httpx.Response(200, text=HTML, headers={"content-type": "text/html"}))
    about = respx.get("https://venue.in/about").mock(
        return_value=httpx.Response(200, text="<p>sales@venue.in</p>", headers={"content-type": "text/html"})
    )
    blocked = respx.get("https://venue.in/contact-us").mock(return_value=httpx.Response(200, text=""))

    found = WebsiteEnricher().enrich("https://venue.in/")

    assert not blocked.called and about.called
    emails = [c.value for c in found if c.kind == "email"]
    assert "sales@venue.in" in emails and "info@venue.in" in emails


@respx.mock
def test_enricher_skips_site_that_disallows_all():
    respx.get("https://closed.in/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    home = respx.get("https://closed.in/").mock(return_value=httpx.Response(200, text=HTML))
    assert WebsiteEnricher().enrich("closed.in") == []
    assert not home.called
