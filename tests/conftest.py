from datetime import UTC, date, datetime, timedelta

import pytest

from invoice_qa.extraction.deterministic import DeterministicExtractor
from invoice_qa.infrastructure.database import Database
from invoice_qa.services.intake import IntakeService
from invoice_qa.services.review import ReviewService

TODAY = date(2026, 10, 10)
FIXED_NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


class MutableClock:
    """Injected database clock: starts at FIXED_NOW and only moves when a test advances it."""

    def __init__(self) -> None:
        self.now = FIXED_NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock():
    return MutableClock()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """A throwaway SQLite path wired through the documented environment variable."""
    path = tmp_path / "invoice_qa_test.sqlite3"
    monkeypatch.setenv("INVOICE_QA_DB_PATH", str(path))
    return path


@pytest.fixture
def database(db_path, clock):
    instance = Database(now_provider=clock)
    instance.initialize()
    return instance


@pytest.fixture
def intake(database):
    return IntakeService(database, DeterministicExtractor(), today_provider=lambda: TODAY)


@pytest.fixture
def review(database):
    return ReviewService(database, today_provider=lambda: TODAY)
