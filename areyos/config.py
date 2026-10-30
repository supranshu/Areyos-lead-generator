from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="AREYOS_", extra="ignore")

    database_url: str = "postgresql+psycopg://areyos:areyos@localhost:5432/areyos"

    # Google Places API (New). Discovery via Google is skipped when no key is set.
    google_places_api_key: str | None = None
    # The city viewport is split into an N x N grid; each tile gets its own text
    # search, because one search returns at most 60 places.
    google_grid_size: int = 2
    google_max_pages: int = 3
    # Website and phone fields are billed at a higher SKU; they are what makes
    # a prospect contactable, so they are on by default.
    google_contact_fields: bool = True

    overpass_url: str = "https://overpass-api.de/api/interpreter"
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    osm_enabled: bool = True

    # Identify the crawler honestly; public APIs (Nominatim, Overpass) require it.
    user_agent: str = "AreyosLeadBot/1.0 (+https://areyos.com)"
    http_timeout: float = 20.0
    # Minimum seconds between requests to the same host.
    per_host_delay: float = 1.0

    enrich_enabled: bool = True
    enrich_max_pages: int = 3
    enrich_refresh_days: int = 30

    # Decision-maker discovery.
    people_enabled: bool = True
    # Only look for people at prospects of at least this priority (high | medium | low).
    people_min_priority: str = "medium"
    people_min_confidence: float = 0.6
    people_max_pages: int = 5
    people_refresh_days: int = 60
    # Web search API for LinkedIn profile hits: "brave" or "google_cse"; unset disables search.
    people_search_provider: str | None = None
    people_max_search_queries: int = 2
    brave_api_key: str | None = None
    google_cse_key: str | None = None
    google_cse_cx: str | None = None
    hunter_api_key: str | None = None

    # Opportunity scoring. Claude reads the evidence when Anthropic credentials are
    # available (ANTHROPIC_API_KEY or an `ant auth login` profile); otherwise rules only.
    opportunity_enabled: bool = True
    opportunity_ai: bool = True
    opportunity_model: str = "claude-opus-5-5"
    opportunity_effort: str = "medium"
    opportunity_max_pages: int = 6
    opportunity_min_priority: str = "medium"

    job_max_attempts: int = 3
    worker_poll_seconds: float = 2.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
