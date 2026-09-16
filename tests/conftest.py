import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

DEFAULT_TEST_DB = "postgresql+psycopg://savethedate:savethedate@localhost:5434/savethedate_test"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Child tables first; TRUNCATE ... CASCADE would reach them anyway, but naming them
# keeps the intent readable.
TABLES = ("attendance", "attendees", "rsvps", "guests", "segments", "events")


@pytest.fixture(scope="session", autouse=True)
def _test_database_url() -> str:
    """Point the app at the test database before any app module reads its settings."""
    url = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DB)
    os.environ["DATABASE_URL"] = url
    return url


def alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return config


@pytest.fixture(scope="session", autouse=True)
def _migrated_schema(_test_database_url: str) -> Iterator[None]:
    """Build the test schema with Alembic rather than `Base.metadata.create_all`.

    The attendee/attendance consistency trigger is created by the migration and is
    invisible to SQLAlchemy's metadata. Under `create_all` the suite would run against
    a schema quietly missing the invariant these tests exist to prove, and a test could
    pass while production rejected the same write.
    """
    engine = create_engine(_test_database_url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()

    command.upgrade(alembic_config(), "head")
    yield


@pytest.fixture(scope="session")
def engine(_migrated_schema: None, _test_database_url: str) -> Iterator[Engine]:
    engine = create_engine(_test_database_url)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """A session alongside the app's own, for setting up rows the API cannot create yet."""
    with Session(engine) as session:
        yield session


@pytest.fixture
def client(_clean_tables: None) -> Iterator[TestClient]:
    # Imported here so the environment above is already in place when settings are built.
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
