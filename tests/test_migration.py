"""A database created by the one-attempt-per-source schema must upgrade without losing evidence."""

import sqlite3

import pytest

from invoice_qa.domain.models import AttemptStatus, CandidateStatus
from invoice_qa.infrastructure import database as database_module
from invoice_qa.infrastructure.database import Database

LEGACY_SCHEMA = """
CREATE TABLE source_documents (
    id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, display_filename TEXT NOT NULL,
    size_bytes INTEGER NOT NULL, duplicate_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE extraction_attempts (
    id TEXT PRIMARY KEY,
    source_document_id TEXT NOT NULL UNIQUE REFERENCES source_documents(id),
    method TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('started', 'succeeded', 'error')),
    error_code TEXT, raw_json TEXT, field_confidence_json TEXT, conflicts_json TEXT,
    created_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE candidates (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES extraction_attempts(id),
    original_raw_json TEXT NOT NULL, original_normalized_json TEXT NOT NULL,
    original_field_confidence_json TEXT NOT NULL, original_conflicts_json TEXT NOT NULL,
    original_overall_confidence_bps INTEGER NOT NULL,
    effective_raw_json TEXT NOT NULL, effective_normalized_json TEXT NOT NULL,
    corrected_fields_json TEXT NOT NULL, confirmed_fields_json TEXT NOT NULL,
    confirmed_distinct_ids_json TEXT NOT NULL, issues_json TEXT NOT NULL, logical_key TEXT,
    status TEXT NOT NULL CHECK (status IN
        ('auto_ready', 'review_required', 'approved', 'rejected')),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    event_type TEXT NOT NULL CHECK (event_type IN ('corrected', 'confirmed',
        'duplicate_confirmed_distinct', 'approval_blocked', 'approved', 'rejected')),
    reviewer_label TEXT NOT NULL, reason TEXT, details_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER review_events_no_update BEFORE UPDATE ON review_events
BEGIN SELECT RAISE(ABORT, 'review events are append-only'); END;
CREATE TRIGGER candidates_original_evidence_immutable BEFORE UPDATE ON candidates
WHEN NEW.original_raw_json IS NOT OLD.original_raw_json
BEGIN SELECT RAISE(ABORT, 'original evidence is immutable'); END;
CREATE TRIGGER attempts_terminal_immutable BEFORE UPDATE ON extraction_attempts
WHEN OLD.status <> 'started'
BEGIN SELECT RAISE(ABORT, 'terminal attempts are immutable'); END;
CREATE TRIGGER attempts_no_delete BEFORE DELETE ON extraction_attempts
BEGIN SELECT RAISE(ABORT, 'attempts cannot be deleted'); END;
"""

RAW = (
    '{"currency":"USD","invoice_date":"2026-09-30","invoice_number":"INV-DEMO-0001",'
    '"po_reference":null,"subtotal":"1,000.00","tax_amount":"80.00",'
    '"total_amount":"1,080.00","vendor_name":"Example Demo Ltd.","vendor_tax_id":"DEMO-TAX-1001"}'
)
NORMALIZED = (
    '{"currency":"USD","invoice_date":"2026-09-30","invoice_number":"INV-DEMO-0001",'
    '"po_reference":null,"subtotal":"1000.00","tax_amount":"80.00",'
    '"total_amount":"1080.00","vendor_name":"Example Demo Ltd.","vendor_tax_id":"DEMO-TAX-1001"}'
)


def _seed_legacy(path):
    connection = sqlite3.connect(path)
    connection.executescript(LEGACY_SCHEMA)
    connection.execute(
        "INSERT INTO source_documents VALUES ('s1', ?, 'ok.pdf', 10, 2, 't')", ("1" * 64,)
    )
    connection.execute(
        "INSERT INTO source_documents VALUES ('s2', ?, 'scan.pdf', 10, 0, 't')", ("2" * 64,)
    )
    connection.execute(
        "INSERT INTO extraction_attempts VALUES "
        "('a1', 's1', 'deterministic', 'succeeded', NULL, ?, '{}', '[]', 't', 't')",
        (RAW,),
    )
    connection.execute(
        "INSERT INTO extraction_attempts VALUES "
        "('a2', 's2', 'deterministic', 'error', 'no_extractable_text', NULL, NULL, NULL, 't', 't')"
    )
    connection.execute(
        "INSERT INTO candidates (id, attempt_id, original_raw_json, original_normalized_json, "
        "original_field_confidence_json, original_conflicts_json, "
        "original_overall_confidence_bps, effective_raw_json, effective_normalized_json, "
        "corrected_fields_json, confirmed_fields_json, confirmed_distinct_ids_json, "
        "issues_json, logical_key, status, created_at, updated_at) "
        "VALUES ('c1', 'a1', ?, ?, '{}', '[]', 10000, ?, ?, '[]', '[]', '[]', '[]', "
        "NULL, 'approved', 't', 't')",
        (RAW, NORMALIZED, RAW, NORMALIZED),
    )
    connection.execute(
        "INSERT INTO review_events (candidate_id, event_type, reviewer_label, reason, "
        "details_json, created_at) VALUES ('c1', 'approved', 'Reviewer A', NULL, '{}', 't')"
    )
    connection.commit()
    connection.close()


@pytest.fixture
def legacy_db(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    _seed_legacy(path)
    return Database(path)


def test_legacy_database_upgrades_in_place_and_keeps_every_row(legacy_db):
    legacy_db.initialize()
    assert [c.id for c in legacy_db.list_candidates()] == ["c1"]
    assert len(legacy_db.list_review_events("c1")) == 1
    first = legacy_db.get_attempt_for_source("s1")
    failed = legacy_db.get_attempt_for_source("s2")
    assert (first.attempt_number, first.status) == (1, AttemptStatus.SUCCEEDED)
    assert (failed.attempt_number, failed.status) == (1, AttemptStatus.ERROR)
    assert failed.error_code == "no_extractable_text"
    assert legacy_db.get_candidate_for_attempt("a1").id == "c1"
    with legacy_db.connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_upgrade_is_idempotent(legacy_db):
    legacy_db.initialize()
    legacy_db.initialize()
    assert len(legacy_db.list_attempts_for_source("s1")) == 1


def test_upgraded_database_allows_retry_after_an_error(legacy_db):
    legacy_db.initialize()
    retry = legacy_db.retry_attempt("s2", "deterministic")
    assert retry.attempt_number == 2
    assert [a.status for a in legacy_db.list_attempts_for_source("s2")] == [
        AttemptStatus.ERROR,
        AttemptStatus.STARTED,
    ]
    with pytest.raises(database_module.RetryNotAllowed):
        legacy_db.retry_attempt("s1", "deterministic")


def test_upgraded_database_keeps_its_audit_triggers(legacy_db):
    legacy_db.initialize()
    with legacy_db.connect() as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("UPDATE review_events SET reason = 'x'")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM review_events")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM extraction_attempts")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("UPDATE candidates SET original_raw_json = '{}'")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("UPDATE extraction_attempts SET error_code = 'x' WHERE id = 'a2'")


def test_legacy_candidates_have_unknown_initial_routing_and_are_excluded_from_the_rate(legacy_db):
    legacy_db.initialize()
    assert legacy_db.get_candidate("c1").initial_status is None
    snapshot = legacy_db.metrics_snapshot()
    assert snapshot["initial_status_counts"] == {"auto_ready": 0, "review_required": 0}
    assert snapshot["initial_routing_unknown"] == 1
    assert snapshot["status_counts"]["approved"] == 1


def test_initial_status_cannot_be_rewritten_on_a_legacy_row_either(legacy_db):
    legacy_db.initialize()
    with legacy_db.connect() as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("UPDATE candidates SET initial_status = 'auto_ready'")
    assert legacy_db.get_candidate("c1").initial_status is None
    assert CandidateStatus.APPROVED is legacy_db.get_candidate("c1").status


def test_a_failing_upgrade_rolls_back_completely(legacy_db):
    with legacy_db.connect() as connection:  # a name clash makes the rebuild fail midway
        connection.execute("CREATE TABLE extraction_attempts_new (x)")
    with pytest.raises(sqlite3.DatabaseError):
        legacy_db.initialize()
    with legacy_db.connect() as connection:
        columns = [r["name"] for r in connection.execute("PRAGMA table_info(extraction_attempts)")]
        assert "attempt_number" not in columns
        assert connection.execute("SELECT COUNT(*) FROM extraction_attempts").fetchone()[0] == 2
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        triggers = {row[0] for row in rows}
        assert {"attempts_no_delete", "attempts_terminal_immutable"} <= triggers
