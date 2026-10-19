"""Shared fixtures.

The suite runs against a real Postgres database, since the app's queries use
Postgres-only SQL. Point TEST_DATABASE_URL at an empty, throwaway database;
its tables are dropped and recreated on every run. Without it, every test
that needs the database is skipped.
"""

import os

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
# app.database reads DATABASE_URL at import time. Set it before that import
# so a developer's .env can never point the tests at a real database; the
# engine it builds is unused, since get_db is overridden below.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL or "postgresql://unused@localhost/unused"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.auth import UserInfo, get_current_user  # noqa: E402
from app.database.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402

USER_ID = "00000000-0000-0000-0000-000000000001"
OTHER_USER_ID = "00000000-0000-0000-0000-000000000002"


@pytest.fixture(scope="session")
def engine():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    engine = create_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def db(engine):
    """A session whose work is rolled back after each test, commits included."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, autoflush=False, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def client(db):
    """A TestClient authenticated as USER_ID and sharing the test's db session."""
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        id=USER_ID, email="test@example.com", provider="email", is_anonymous=False
    )
    yield TestClient(app)
    app.dependency_overrides.clear()
