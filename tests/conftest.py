from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    # `app.models` pulls in `app.db`, which builds the engine at import time. Importing
    # it for real up here would read the settings before `_test_database_url` has
    # pointed them at the test database — the same ordering trap as `app.main` below.
    from app.models import Host

DEFAULT_TEST_DB = "postgresql+psycopg://savethedate:savethedate@localhost:5434/savethedate_test"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Child tables first; TRUNCATE ... CASCADE would reach them anyway, but naming them
# keeps the intent readable.
TABLES = (
    "attendance",
    "attendees",
    "deliveries",
    "rsvps",
    "guests",
    "segments",
    "events",
    "host_sessions",
    "hosts",
)


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


@pytest.fixture(autouse=True)
def _clean_rate_limiter() -> None:
    """Drop the throttle's counters between tests. TAP-7727.

    They live in process memory, and every `TestClient` request arrives from the same
    address, so without this the whole suite shares one bucket: enough guest-page tests
    run in a minute to spend the allowance, and a later test gets a 429 it never asked
    for. That is what happened — one test passed alone and failed in the full run.
    """
    from app.ratelimit import reset_limiter

    reset_limiter()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """A session alongside the app's own, for setting up rows the API cannot create yet."""
    with Session(engine) as session:
        yield session


# The host account every suite that touches a host endpoint signs in as. TAP-7725.
HOST_EMAIL = "host@example.com"
HOST_PASSWORD = "a correct horse battery staple"


@pytest.fixture
def anonymous_client(_clean_tables: None) -> Iterator[TestClient]:
    """A client holding no session cookie, and its OWN cookie jar.

    Host endpoints must answer 401 to this. It is a separate `TestClient` from `client`
    on purpose: the first version derived one from the other, so signing in through
    `client` also signed in `anonymous_client` — they were the same object — and three
    401 tests passed a 200 straight through. A test for "no credentials" has to hold
    no credentials.
    """
    # Imported here so the environment above is already in place when settings are built.
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def host(engine: Engine, _clean_tables: None) -> Host:
    """One registered host, written directly.

    Not through `POST /auth/register`, because that endpoint is deliberately closed
    unless a bootstrap token is configured, and its own tests cover it. This fixture
    exists so the other ninety-odd tests can get past the door.
    """
    from app.auth import hash_password
    from app.models import Host

    with Session(engine) as session:
        row = Host(email=HOST_EMAIL, password_hash=hash_password(HOST_PASSWORD))
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


@pytest.fixture
def client(_clean_tables: None, host: Host) -> Iterator[TestClient]:
    """A signed-in host client — what most of the suite wants.

    Signing in for real rather than overriding the dependency, so the cookie, the
    session row and the expiry are all exercised by every test that uses this.
    """
    from app.main import app

    with TestClient(app) as signed_in:
        yield _sign_in(signed_in)


def _sign_in(test_client: TestClient) -> TestClient:
    response = test_client.post(
        "/auth/login", json={"email": HOST_EMAIL, "password": HOST_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return test_client
