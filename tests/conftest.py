import os
from pathlib import Path

import pytest
from sqlalchemy import text

TEST_DB = os.environ.get("AREYOS_TEST_DATABASE_URL", "postgresql+psycopg://areyos:areyos@localhost:5432/areyos_test")
os.environ["AREYOS_DATABASE_URL"] = TEST_DB
os.environ["AREYOS_PER_HOST_DELAY"] = "0"
os.environ.setdefault("AREYOS_GOOGLE_PLACES_API_KEY", "test-key")

from areyos import db  # noqa: E402
from areyos.config import get_settings  # noqa: E402
from areyos.engine import registry  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    from alembic import command
    from alembic.config import Config

    get_settings.cache_clear()
    db.reset_engine()
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    yield
    db.reset_engine()


@pytest.fixture(autouse=True)
def clean_db():
    with db.get_engine().begin() as conn:
        conn.execute(
            text(
                "TRUNCATE prospects, prospect_sources, contacts, discovery_runs, run_prospects, jobs, geo_cache, "
                "people, person_contacts, person_sources, suppressions, opportunity_assessments, opportunity_reports, "
                "sales_reps, activities, follow_ups "
                "RESTART IDENTITY CASCADE"
            )
        )
    registry._cache.clear()
    yield
    registry._cache.clear()


@pytest.fixture
def session():
    with db.session_scope() as s:
        yield s
