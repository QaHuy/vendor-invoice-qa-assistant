import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from invoice_qa.domain.models import (
    AttemptStatus,
    CandidateStatus,
    FieldConflict,
    IssueCode,
    NormalizedInvoiceFields,
    RawInvoiceFields,
    ReviewEventType,
    Severity,
    ValidationIssue,
)
from invoice_qa.domain.normalization import normalize_invoice
from invoice_qa.domain.validation import logical_duplicate_key
from invoice_qa.infrastructure import database as database_module
from invoice_qa.infrastructure.database import STALE_ATTEMPT_AFTER, Database

FIXED_NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

RAW = RawInvoiceFields(
    vendor_name="Example Demo Office Supplies Ltd.",
    vendor_tax_id="DEMO-TAX-1001",
    invoice_number="INV-DEMO-0001",
    invoice_date="2026-09-30",
    currency="USD",
    subtotal="1,000.00",
    tax_amount="80.00",
    total_amount="1,080.00",
    po_reference="PO-DEMO-001",
)
CONFIDENCE = {
    "vendor_name": 10000,
    "invoice_number": 10000,
    "invoice_date": 10000,
    "currency": 10000,
    "subtotal": 10000,
    "tax_amount": 10000,
    "total_amount": 10000,
    "vendor_tax_id": 10000,
    "po_reference": 10000,
}


@pytest.fixture
def db(db_path, clock):
    database = Database(now_provider=clock)
    database.initialize()
    return database


def complete(db, attempt_id, raw=RAW, status=CandidateStatus.AUTO_READY, issues=None, conflicts=()):
    """Finish a started attempt and create its candidate in the single atomic database call."""
    normalized = normalize_invoice(raw).fields
    return db.complete_attempt_with_candidate(
        attempt_id,
        raw=raw,
        field_confidence_bps=CONFIDENCE,
        conflicts=list(conflicts),
        original_normalized=normalized,
        original_overall_confidence_bps=10000,
        issues=issues or [],
        status=status,
        logical_key=logical_duplicate_key(normalized),
    )


def make_candidate(db, sha="a" * 64, raw=RAW, status=CandidateStatus.AUTO_READY, issues=None):
    source, _ = db.get_or_create_source_document(sha, "invoice.pdf", 1234)
    attempt = db.create_attempt(source.id, "deterministic")
    return complete(db, attempt.id, raw=raw, status=status, issues=issues)


def apply_change(db, candidate, **overrides):
    args = {
        "effective_raw": candidate.effective_raw,
        "effective_normalized": candidate.effective_normalized,
        "corrected_fields": candidate.corrected_fields,
        "confirmed_fields": candidate.confirmed_fields,
        "confirmed_distinct_candidate_ids": candidate.confirmed_distinct_candidate_ids,
        "issues": candidate.issues,
        "status": candidate.status,
        "logical_key": logical_duplicate_key(candidate.effective_normalized),
        "event_type": ReviewEventType.CORRECTED,
        "reviewer_label": "Reviewer A",
        "reason": None,
        "details": {},
    }
    args.update(overrides)
    return db.apply_review_change_with_event(candidate.id, **args)


def test_schema_initializes_twice(db):
    db.initialize()
    db.initialize()


def test_database_path_comes_from_environment(db_path):
    database = Database()
    database.initialize()
    assert db_path.exists()


def test_each_operation_opens_and_closes_its_own_connection(db, monkeypatch):
    opened = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(database_module.sqlite3, "connect", spy)
    db.get_or_create_source_document("b" * 64, "x.pdf", 10)
    db.list_candidates()
    assert len(opened) == 2
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_connections_enforce_foreign_keys(db):
    with db.connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO extraction_attempts (id, source_document_id, attempt_number, method, "
                "status, created_at) VALUES ('x', 'missing', 1, 'deterministic', 'started', 't')"
            )


def test_source_sha_is_unique_and_reused(db):
    first, created_first = db.get_or_create_source_document("c" * 64, "a.pdf", 5)
    second, created_second = db.get_or_create_source_document("c" * 64, "other.pdf", 5)
    assert created_first and not created_second
    assert first.id == second.id
    assert second.display_filename == "a.pdf"
    with db.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO source_documents (id, sha256, display_filename, size_bytes, "
                "duplicate_count, created_at) VALUES ('z', ?, 'f', 1, 0, 't')",
                ("c" * 64,),
            )


def test_second_attempt_while_the_first_is_in_flight_is_rejected(db):
    source, _ = db.get_or_create_source_document("d" * 64, "a.pdf", 5)
    db.create_attempt(source.id, "deterministic")
    with pytest.raises(sqlite3.IntegrityError):
        db.create_attempt(source.id, "deterministic")


def test_attempts_are_numbered_and_every_failed_attempt_is_preserved(db):
    source, _ = db.get_or_create_source_document("d1" * 32, "a.pdf", 5)
    first = db.create_attempt(source.id, "deterministic")
    assert first.attempt_number == 1
    db.finish_attempt_error(first.id, "no_extractable_text")
    second = db.retry_attempt(source.id, "deterministic")
    assert second.attempt_number == 2
    db.finish_attempt_error(second.id, "extraction_failed")
    third = db.retry_attempt(source.id, "deterministic")
    assert third.attempt_number == 3
    attempts = db.list_attempts_for_source(source.id)
    assert [(a.attempt_number, a.status, a.error_code) for a in attempts] == [
        (1, AttemptStatus.ERROR, "no_extractable_text"),
        (2, AttemptStatus.ERROR, "extraction_failed"),
        (3, AttemptStatus.STARTED, None),
    ]
    assert db.get_attempt_for_source(source.id).id == third.id


def test_failed_attempts_stay_immutable_after_a_retry(db):
    source, _ = db.get_or_create_source_document("d2" * 32, "a.pdf", 5)
    first = db.create_attempt(source.id, "deterministic")
    db.finish_attempt_error(first.id, "no_extractable_text")
    db.retry_attempt(source.id, "deterministic")
    with pytest.raises(sqlite3.DatabaseError):
        db.finish_attempt_error(first.id, "rewritten")
    with pytest.raises(database_module.AttemptNotActive):
        complete(db, first.id)
    with db.connect() as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM extraction_attempts WHERE id = ?", (first.id,))
    assert db.list_attempts_for_source(source.id)[0].error_code == "no_extractable_text"


def test_retry_of_a_stale_started_attempt_preserves_it_as_interrupted(db, clock):
    source, _ = db.get_or_create_source_document("d3" * 32, "a.pdf", 5)
    stranded = db.create_attempt(source.id, "deterministic")
    clock.advance(seconds=STALE_ATTEMPT_AFTER.total_seconds())
    retry = db.retry_attempt(source.id, "deterministic")
    attempts = db.list_attempts_for_source(source.id)
    assert [a.id for a in attempts] == [stranded.id, retry.id]
    assert attempts[0].status is AttemptStatus.ERROR
    assert attempts[0].error_code == "extraction_interrupted"
    assert attempts[1].status is AttemptStatus.STARTED


def test_retry_is_refused_after_a_successful_attempt(db):
    candidate = make_candidate(db, sha="d4" * 32)
    source = db.get_or_create_source_document("d4" * 32, "invoice.pdf", 1234)[0]
    with pytest.raises(database_module.RetryNotAllowed):
        db.retry_attempt(source.id, "deterministic")
    assert len(db.list_attempts_for_source(source.id)) == 1
    assert db.get_candidate(candidate.id) is not None


def test_retry_is_refused_for_an_unknown_source_and_creates_nothing(db):
    with pytest.raises(database_module.RetryNotAllowed):
        db.retry_attempt("missing", "deterministic")
    assert db.metrics_snapshot()["attempts"] == 0


def test_retry_of_a_source_that_crashed_before_its_first_attempt_starts_attempt_one(db):
    source, _ = db.get_or_create_source_document("d5" * 32, "a.pdf", 5)
    assert db.get_attempt_for_source(source.id) is None
    assert db.retry_attempt(source.id, "deterministic").attempt_number == 1


def test_a_source_can_never_hold_two_succeeded_attempts(db):
    make_candidate(db, sha="d6" * 32)
    source = db.get_or_create_source_document("d6" * 32, "invoice.pdf", 1234)[0]
    with pytest.raises(sqlite3.IntegrityError):
        db.create_attempt(source.id, "deterministic")


def test_initial_status_is_persisted_and_immutable(db):
    candidate = make_candidate(db, status=CandidateStatus.REVIEW_REQUIRED)
    assert candidate.initial_status is CandidateStatus.REVIEW_REQUIRED
    changed = apply_change(
        db,
        candidate,
        status=CandidateStatus.REJECTED,
        event_type=ReviewEventType.REJECTED,
        reason="duplicate",
    )
    assert changed.status is CandidateStatus.REJECTED
    assert changed.initial_status is CandidateStatus.REVIEW_REQUIRED
    with db.connect() as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "UPDATE candidates SET initial_status = 'auto_ready' WHERE id = ?",
                (candidate.id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE candidates SET initial_status = 'bogus' WHERE id = ?", (candidate.id,)
            )


def test_terminal_attempts_are_immutable(db):
    source, _ = db.get_or_create_source_document("e" * 64, "a.pdf", 5)
    attempt = db.create_attempt(source.id, "deterministic")
    assert attempt.status is AttemptStatus.STARTED
    db.finish_attempt_error(attempt.id, "no_extractable_text")
    stored = db.get_attempt_for_source(source.id)
    assert stored.status is AttemptStatus.ERROR
    assert stored.error_code == "no_extractable_text"
    with pytest.raises(database_module.AttemptNotActive):
        complete(db, attempt.id)
    with pytest.raises(sqlite3.DatabaseError):
        db.finish_attempt_error(attempt.id, "other")
    with db.connect() as connection, pytest.raises(sqlite3.DatabaseError):
        connection.execute(
            "UPDATE extraction_attempts SET status = 'succeeded' WHERE id = ?", (attempt.id,)
        )
    assert db.get_attempt_for_source(source.id).error_code == "no_extractable_text"


def test_successful_attempt_keeps_raw_extraction(db):
    candidate = make_candidate(db)
    attempt = db.get_attempt_for_source(
        db.get_or_create_source_document("a" * 64, "invoice.pdf", 1234)[0].id
    )
    assert attempt.status is AttemptStatus.SUCCEEDED
    assert attempt.raw == RAW
    assert attempt.field_confidence_bps == CONFIDENCE
    assert candidate.attempt_id == attempt.id
    with pytest.raises(sqlite3.DatabaseError):
        db.finish_attempt_error(attempt.id, "late_failure")


def test_missing_attempt_returns_none(db):
    source, _ = db.get_or_create_source_document("f" * 64, "a.pdf", 5)
    assert db.get_attempt_for_source(source.id) is None


def test_candidate_round_trips_every_stored_field(db):
    issue = ValidationIssue(
        code=IssueCode.LOW_FIELD_CONFIDENCE,
        field_name="vendor_name",
        severity=Severity.REVIEW_WARNING,
        resolution="Reviewer corrects or confirms the field.",
        message="Extraction confidence for this field is low.",
    )
    candidate = make_candidate(db, status=CandidateStatus.REVIEW_REQUIRED, issues=[issue])
    loaded = db.get_candidate(candidate.id)
    assert loaded == candidate
    assert loaded.original_raw == RAW
    assert loaded.original_normalized.total_amount == Decimal("1080.00")
    assert loaded.original_normalized.invoice_date == date(2026, 9, 30)
    assert loaded.effective_raw == RAW
    assert loaded.original_field_confidence_bps == CONFIDENCE
    assert loaded.original_overall_confidence_bps == 10000
    assert loaded.corrected_fields == set()
    assert loaded.confirmed_fields == set()
    assert loaded.confirmed_distinct_candidate_ids == set()
    assert loaded.issues == [issue]
    assert loaded.status is CandidateStatus.REVIEW_REQUIRED
    assert loaded.created_at == FIXED_NOW.isoformat()


def test_conflicts_round_trip(db):
    source, _ = db.get_or_create_source_document("1" * 64, "a.pdf", 5)
    attempt = db.create_attempt(source.id, "deterministic")
    conflict = FieldConflict(field_name="total_amount", values=["1", "2"])
    candidate = complete(
        db, attempt.id, status=CandidateStatus.REVIEW_REQUIRED, conflicts=[conflict]
    )
    assert db.get_candidate(candidate.id).conflicts == [conflict]
    assert db.get_attempt_for_source(source.id).conflicts == [conflict]


def test_unknown_candidate_returns_none(db):
    assert db.get_candidate("missing") is None


def test_list_candidates_orders_by_creation_and_filters_by_status(db):
    first = make_candidate(db, sha="1" * 64)
    second = make_candidate(db, sha="2" * 64, status=CandidateStatus.REVIEW_REQUIRED)
    assert [c.id for c in db.list_candidates()] == [first.id, second.id]
    only_review = db.list_candidates(CandidateStatus.REVIEW_REQUIRED)
    assert [c.id for c in only_review] == [second.id]


@pytest.mark.parametrize(
    "column",
    [
        "original_raw_json",
        "original_normalized_json",
        "original_field_confidence_json",
        "original_conflicts_json",
        "original_overall_confidence_bps",
    ],
)
def test_direct_sql_cannot_change_original_evidence(db, column):
    candidate = make_candidate(db)
    value = 1 if column.endswith("_bps") else "{}"
    with db.connect() as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                f"UPDATE candidates SET {column} = ? WHERE id = ?", (value, candidate.id)
            )
    assert db.get_candidate(candidate.id) == candidate


def test_direct_sql_can_still_change_effective_values(db):
    candidate = make_candidate(db)
    with db.connect() as connection:
        connection.execute(
            "UPDATE candidates SET status = 'rejected' WHERE id = ?", (candidate.id,)
        )
    assert db.get_candidate(candidate.id).status is CandidateStatus.REJECTED


def test_decimal_and_date_json_round_trip_exactly(db):
    raw = RAW.model_copy(update={"tax_amount": "80.5"})
    candidate = make_candidate(db, raw=raw)
    stored = db.get_candidate(candidate.id).original_normalized
    assert str(stored.tax_amount) == "80.50"
    assert isinstance(stored, NormalizedInvoiceFields)
    assert stored.invoice_date == date(2026, 9, 30)


def test_duplicate_observation_increments_without_changing_evidence(db):
    candidate = make_candidate(db)
    source, _ = db.get_or_create_source_document("a" * 64, "again.pdf", 1234)
    assert source.duplicate_count == 0
    assert db.record_duplicate_observation(source.id) == 1
    assert db.record_duplicate_observation(source.id) == 2
    reloaded, _ = db.get_or_create_source_document("a" * 64, "again.pdf", 1234)
    assert reloaded.duplicate_count == 2
    assert reloaded.display_filename == "invoice.pdf"
    assert db.get_candidate(candidate.id) == candidate


def test_apply_review_change_updates_effective_values_and_appends_one_event(db):
    candidate = make_candidate(db)
    corrected_raw = RAW.model_copy(update={"invoice_number": "INV-DEMO-0002"})
    updated = apply_change(
        db,
        candidate,
        effective_raw=corrected_raw,
        effective_normalized=normalize_invoice(corrected_raw).fields,
        corrected_fields={"invoice_number"},
        status=CandidateStatus.REVIEW_REQUIRED,
        details={
            "changes": {"invoice_number": {"before": "INV-DEMO-0001", "after": "INV-DEMO-0002"}}
        },
    )
    assert updated.effective_raw.invoice_number == "INV-DEMO-0002"
    assert updated.original_raw.invoice_number == "INV-DEMO-0001"
    assert updated.corrected_fields == {"invoice_number"}
    assert updated.status is CandidateStatus.REVIEW_REQUIRED
    events = db.list_review_events(candidate.id)
    assert len(events) == 1
    assert events[0].event_type is ReviewEventType.CORRECTED
    assert events[0].reviewer_label == "Reviewer A"
    assert events[0].details["changes"]["invoice_number"]["after"] == "INV-DEMO-0002"


def test_review_events_return_in_append_order(db):
    candidate = make_candidate(db)
    apply_change(db, candidate, event_type=ReviewEventType.CONFIRMED)
    apply_change(db, candidate, event_type=ReviewEventType.APPROVAL_BLOCKED)
    apply_change(db, candidate, event_type=ReviewEventType.REJECTED, reason="duplicate")
    events = db.list_review_events(candidate.id)
    assert [e.event_type for e in events] == [
        ReviewEventType.CONFIRMED,
        ReviewEventType.APPROVAL_BLOCKED,
        ReviewEventType.REJECTED,
    ]
    assert events[2].reason == "duplicate"


def test_direct_sql_cannot_update_or_delete_review_events(db):
    candidate = make_candidate(db)
    apply_change(db, candidate)
    with db.connect() as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("UPDATE review_events SET reviewer_label = 'x'")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM review_events")
    assert len(db.list_review_events(candidate.id)) == 1


def test_correction_and_event_roll_back_together_on_failure(db):
    candidate = make_candidate(db)
    changed = RAW.model_copy(update={"invoice_number": "INV-DEMO-9999"})
    with pytest.raises(sqlite3.DatabaseError):
        apply_change(
            db,
            candidate,
            effective_raw=changed,
            effective_normalized=normalize_invoice(changed).fields,
            status=CandidateStatus.REJECTED,
            event_type="not_an_event_type",
        )
    assert db.get_candidate(candidate.id) == candidate
    assert db.list_review_events(candidate.id) == []


def test_find_logical_duplicates_excludes_self_and_rejected(db):
    first = make_candidate(db, sha="1" * 64)
    second = make_candidate(db, sha="2" * 64, status=CandidateStatus.REVIEW_REQUIRED)
    third = make_candidate(db, sha="3" * 64)
    key = logical_duplicate_key(first.effective_normalized)
    assert db.find_logical_duplicates(key, exclude_candidate_id=first.id) == sorted(
        [second.id, third.id]
    )
    apply_change(db, third, status=CandidateStatus.REJECTED, event_type=ReviewEventType.REJECTED)
    assert db.find_logical_duplicates(key, exclude_candidate_id=first.id) == [second.id]
    assert db.find_logical_duplicates(None, exclude_candidate_id=first.id) == []


def test_approved_candidates_still_count_as_logical_duplicates(db):
    first = make_candidate(db, sha="1" * 64)
    second = make_candidate(db, sha="2" * 64)
    apply_change(db, second, status=CandidateStatus.APPROVED, event_type=ReviewEventType.APPROVED)
    key = logical_duplicate_key(first.effective_normalized)
    assert db.find_logical_duplicates(key, exclude_candidate_id=first.id) == [second.id]


def test_metrics_snapshot_counts_facts(db):
    empty = db.metrics_snapshot()
    assert empty["sources"] == 0
    assert empty["average_original_confidence_bps"] is None
    make_candidate(db, sha="1" * 64)
    flagged = ValidationIssue(
        code=IssueCode.POSSIBLE_LOGICAL_DUPLICATE,
        field_name=None,
        severity=Severity.BLOCKING,
        resolution="r",
        message="m",
        candidate_ids=["x"],
    )
    make_candidate(db, sha="2" * 64, status=CandidateStatus.REVIEW_REQUIRED, issues=[flagged])
    failed_source, _ = db.get_or_create_source_document("3" * 64, "bad.pdf", 3)
    failed = db.create_attempt(failed_source.id, "deterministic")
    db.finish_attempt_error(failed.id, "no_extractable_text")
    db.record_duplicate_observation(failed_source.id)
    snapshot = db.metrics_snapshot()
    assert snapshot["sources"] == 3
    assert snapshot["attempts"] == 3
    assert snapshot["extraction_errors"] == 1
    assert snapshot["content_duplicates"] == 1
    assert snapshot["successful_candidates"] == 2
    assert snapshot["logical_duplicate_flags"] == 1
    assert snapshot["status_counts"] == {
        "auto_ready": 1,
        "review_required": 1,
        "approved": 0,
        "rejected": 0,
    }
    assert snapshot["average_original_confidence_bps"] == 10000


def test_candidate_lookup_by_attempt(db):
    candidate = make_candidate(db)
    assert db.get_candidate_for_attempt(candidate.attempt_id) == candidate
    assert db.get_candidate_for_attempt("missing") is None


# ----------------------------------------------- atomic success (attempt + candidate together)


class _BrokenStatus:
    """Injected failure: the candidate INSERT violates its status CHECK constraint."""

    value = "not_a_status"


def test_a_failed_candidate_insert_rolls_back_the_attempt_success(db):
    source, _ = db.get_or_create_source_document("f1" * 32, "a.pdf", 5)
    attempt = db.create_attempt(source.id, "deterministic")
    with pytest.raises(sqlite3.IntegrityError):
        complete(db, attempt.id, status=_BrokenStatus())
    stored = db.get_attempt_for_source(source.id)
    assert stored.status is AttemptStatus.STARTED
    assert stored.raw is None and stored.field_confidence_bps == {}
    assert db.list_candidates() == []
    assert db.metrics_snapshot()["successful_candidates"] == 0


def test_a_failure_inside_candidate_creation_leaves_no_succeeded_attempt(db, monkeypatch):
    source, _ = db.get_or_create_source_document("f2" * 32, "a.pdf", 5)
    attempt = db.create_attempt(source.id, "deterministic")

    def explode(*args, **kwargs):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(db, "_insert_candidate", explode)
    with pytest.raises(RuntimeError):
        complete(db, attempt.id)
    assert db.get_attempt_for_source(source.id).status is AttemptStatus.STARTED
    assert db.list_candidates() == []
    with db.connect() as connection:
        orphans = connection.execute(
            "SELECT COUNT(*) FROM extraction_attempts a WHERE a.status = 'succeeded' "
            "AND NOT EXISTS (SELECT 1 FROM candidates c WHERE c.attempt_id = a.id)"
        ).fetchone()[0]
    assert orphans == 0


def test_success_and_candidate_commit_together_with_initial_status(db):
    source, _ = db.get_or_create_source_document("f3" * 32, "a.pdf", 5)
    attempt = db.create_attempt(source.id, "deterministic")
    candidate = complete(db, attempt.id, status=CandidateStatus.REVIEW_REQUIRED)
    stored = db.get_attempt_for_source(source.id)
    assert stored.status is AttemptStatus.SUCCEEDED and stored.raw == RAW
    assert candidate.attempt_id == attempt.id
    assert candidate.initial_status is CandidateStatus.REVIEW_REQUIRED
    assert db.get_candidate_for_attempt(attempt.id).id == candidate.id


def test_completion_requires_the_attempt_to_still_be_started(db):
    source, _ = db.get_or_create_source_document("f4" * 32, "a.pdf", 5)
    attempt = db.create_attempt(source.id, "deterministic")
    complete(db, attempt.id)
    with pytest.raises(database_module.AttemptNotActive):
        complete(db, attempt.id)
    with pytest.raises(database_module.AttemptNotActive):
        complete(db, "missing-attempt")
    assert len(db.list_candidates()) == 1


# ------------------------------------------- recovery of a succeeded attempt without candidate


def strand_succeeded_attempt(db, sha):
    """Reproduce the old crash window: a succeeded attempt (with evidence) but no candidate."""
    source, _ = db.get_or_create_source_document(sha, "a.pdf", 5)
    attempt = db.create_attempt(source.id, "deterministic")
    with db.connect() as connection:
        connection.execute(
            "UPDATE extraction_attempts SET status = 'succeeded', raw_json = ?, "
            "field_confidence_json = ?, conflicts_json = '[]', finished_at = ? WHERE id = ?",
            (
                json.dumps(RAW.to_json_dict()),
                json.dumps(CONFIDENCE),
                FIXED_NOW.isoformat(),
                attempt.id,
            ),
        )
    return source, attempt


def attach(db, attempt_id, status=CandidateStatus.AUTO_READY):
    normalized = normalize_invoice(RAW).fields
    return db.attach_candidate_to_succeeded_attempt(
        attempt_id,
        original_normalized=normalized,
        original_overall_confidence_bps=10000,
        issues=[],
        status=status,
        logical_key=logical_duplicate_key(normalized),
    )


def test_recovery_rebuilds_the_candidate_from_the_stored_raw_evidence(db):
    source, attempt = strand_succeeded_attempt(db, "f5" * 32)
    assert db.get_candidate_for_attempt(attempt.id) is None
    candidate = attach(db, attempt.id)
    assert candidate.attempt_id == attempt.id
    assert candidate.original_raw == RAW
    assert candidate.original_field_confidence_bps == CONFIDENCE
    assert candidate.initial_status is CandidateStatus.AUTO_READY
    assert len(db.list_attempts_for_source(source.id)) == 1


def test_recovery_is_idempotent(db):
    _, attempt = strand_succeeded_attempt(db, "f6" * 32)
    first = attach(db, attempt.id)
    second = attach(db, attempt.id, status=CandidateStatus.REVIEW_REQUIRED)
    assert second.id == first.id and second.initial_status is CandidateStatus.AUTO_READY
    assert len(db.list_candidates()) == 1


def test_recovery_refuses_an_attempt_that_did_not_succeed(db):
    source, _ = db.get_or_create_source_document("f7" * 32, "a.pdf", 5)
    started = db.create_attempt(source.id, "deterministic")
    with pytest.raises(database_module.AttemptNotActive):
        attach(db, started.id)
    with pytest.raises(database_module.AttemptNotActive):
        attach(db, "missing-attempt")


# ------------------------------------------------ active attempts are never interrupted early


def test_a_recent_started_attempt_blocks_retry_and_is_not_modified(db, clock):
    source, _ = db.get_or_create_source_document("f8" * 32, "a.pdf", 5)
    running = db.create_attempt(source.id, "deterministic")
    clock.advance(seconds=STALE_ATTEMPT_AFTER.total_seconds() - 1)
    with pytest.raises(database_module.AttemptInProgress):
        db.retry_attempt(source.id, "deterministic")
    attempts = db.list_attempts_for_source(source.id)
    assert [(a.id, a.status, a.error_code) for a in attempts] == [
        (running.id, AttemptStatus.STARTED, None)
    ]
    assert not db.is_attempt_stale(attempts[0])


def test_the_stale_threshold_is_documented_and_boundary_inclusive(db, clock):
    assert STALE_ATTEMPT_AFTER == timedelta(minutes=10)
    source, _ = db.get_or_create_source_document("f9" * 32, "a.pdf", 5)
    db.create_attempt(source.id, "deterministic")
    clock.advance(minutes=10)
    assert db.is_attempt_stale(db.get_attempt_for_source(source.id))
    retry = db.retry_attempt(source.id, "deterministic")
    assert retry.attempt_number == 2


def test_the_stale_threshold_can_be_configured(db_path, clock):
    short = Database(now_provider=clock, stale_attempt_after=timedelta(seconds=30))
    short.initialize()
    source, _ = short.get_or_create_source_document("fa" * 32, "a.pdf", 5)
    short.create_attempt(source.id, "deterministic")
    clock.advance(seconds=29)
    with pytest.raises(database_module.AttemptInProgress):
        short.retry_attempt(source.id, "deterministic")
    clock.advance(seconds=1)
    assert short.retry_attempt(source.id, "deterministic").attempt_number == 2
