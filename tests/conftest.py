import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

DEFAULT_TEST_DB = "postgresql+psycopg://savethedate:savethedate@localhost:5434/savethedate_test"


@pytest.fixture(scope="session", autouse=True)
def _test_database_url() -> str:
    """Point the app at the test database before any app module reads its settings."""
    url = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DB)
    os.environ["DATABASE_URL"] = url
    return url


@pytest.fixture
def client(_test_database_url: str) -> Iterator[TestClient]:
    # Imported here so the environment above is already in place when settings are built.
    from app.db import Base, engine
    from app.main import app

    Base.metadata.create_all(engine)
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        Base.metadata.drop_all(engine)
