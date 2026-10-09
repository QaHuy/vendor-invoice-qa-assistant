"""Runtime configuration read from the process environment only."""

import os
from datetime import UTC, date, datetime
from pathlib import Path

DB_PATH_ENV = "INVOICE_QA_DB_PATH"
DEFAULT_DB_PATH = ".data/invoice_qa.sqlite3"


def get_db_path() -> Path:
    return Path(os.environ.get(DB_PATH_ENV, DEFAULT_DB_PATH))


def default_today() -> date:
    """Default clock: the current UTC date. Services receive it by injection."""
    return datetime.now(UTC).date()
