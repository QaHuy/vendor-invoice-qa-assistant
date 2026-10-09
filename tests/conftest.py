from datetime import UTC, date, datetime

import pytest

from invoice_qa.extraction.deterministic import DeterministicExtractor
from invoice_qa.infrastructure.database import Database
from invoice_qa.services.intake import IntakeService
from invoice_qa.services.review import ReviewService

TODAY = date(2026, 10, 10)
FIXED_NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """A throwaway SQLite path wired through the documented environment variable."""
    path = tmp_path / "invoice_qa_test.sqlite3"
    monkeypatch.setenv("INVOICE_QA_DB_PATH", str(path))
    return path


@pytest.fixture
def database(db_path):
    instance = Database(now_provider=lambda: FIXED_NOW)
    instance.initialize()
    return instance


@pytest.fixture
def intake(database):
    return IntakeService(database, DeterministicExtractor(), today_provider=lambda: TODAY)


@pytest.fixture
def review(database):
    return ReviewService(database, today_provider=lambda: TODAY)
