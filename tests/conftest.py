import pytest


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """A throwaway SQLite path wired through the documented environment variable."""
    path = tmp_path / "invoice_qa_test.sqlite3"
    monkeypatch.setenv("INVOICE_QA_DB_PATH", str(path))
    return path
