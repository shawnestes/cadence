import os

# Must run before any `app` import because get_settings() requires CADENCE_DATABASE_URL.
os.environ.setdefault(
    "CADENCE_DATABASE_URL",
    "postgresql+asyncpg://test:test@localhost:5432/test_cadence",
)

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c
