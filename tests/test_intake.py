import json
import threading
from datetime import date

from conftest import FIXED_NOW, TODAY
from factories import encrypted_pdf, make_pdf, malformed_pdf, no_text_pdf
from fakes import flaky_service
from invoice_qa.domain.models import AttemptStatus, CandidateStatus, IssueCode
from invoice_qa.extraction.base import ExtractionResult
from invoice_qa.extraction.deterministic import DeterministicExtractor
from invoice_qa.infrastructure.database import STALE_ATTEMPT_AFTER, Database
from invoice_qa.infrastructure.file_security import sha256_bytes
from invoice_qa.services.intake import IntakeOutcome, IntakeService


def codes(candidate) -> set[IssueCode]:
    return {issue.code for issue in candidate.issues}


def test_clean_extraction_creates_auto_ready_candidate(intake):
    result = intake.process_upload("invoice.pdf", make_pdf())
    assert result.outcome is IntakeOutcome.CANDIDATE
    assert not result.content_duplicate
    candidate = result.candidate
    assert candidate.status is CandidateStatus.AUTO_READY
    assert candidate.issues == []
    assert candidate.original_overall_confidence_bps == 10000
    assert candidate.original_raw.invoice_number == "INV-DEMO-0001"
    assert str(candidate.original_normalized.total_amount) == "1080.00"


def test_invalid_extraction_creates_review_required_candidate(intake):
    result = intake.process_upload("invoice.pdf", make_pdf({"Subtotal": "1,0000"}))
    assert result.candidate.status is CandidateStatus.REVIEW_REQUIRED
    assert IssueCode.MALFORMED_VALUE in codes(result.candidate)
    assert result.candidate.original_raw.subtotal == "1,0000"


def test_low_confidence_extraction_creates_review_required_candidate(intake):
    pdf = make_pdf({"Vendor Name": None}, ["Vendor_Name: Example Demo Loose Ltd."])
    candidate = intake.process_upload("invoice.pdf", pdf).candidate
    assert candidate.status is CandidateStatus.REVIEW_REQUIRED
    assert codes(candidate) == {IssueCode.LOW_FIELD_CONFIDENCE}


def test_conflicting_total_is_routed_to_review_with_conflict_evidence(intake):
    candidate = intake.process_upload(
        "invoice.pdf", make_pdf(extra=["Total Amount: 1,090.00"])
    ).candidate
    assert candidate.status is CandidateStatus.REVIEW_REQUIRED
    assert IssueCode.CONFLICTING_VALUES in codes(candidate)
    assert candidate.conflicts[0].values == ["1,080.00", "1,090.00"]


def test_extraction_failure_records_error_attempt_and_no_candidate(intake, database):
    result = intake.process_upload("scan.pdf", no_text_pdf())
    assert result.outcome is IntakeOutcome.EXTRACTION_ERROR
    assert result.error_code == "no_extractable_text"
    assert result.candidate is None
    assert database.list_candidates() == []
    attempt = database.get_attempt_for_source(result.source_document_id)
    assert attempt.status is AttemptStatus.ERROR
    assert attempt.error_code == "no_extractable_text"


def test_extractor_exception_becomes_generic_extraction_failed(database):
    class Exploding:
        def extract(self, pdf_bytes: bytes) -> ExtractionResult:
            raise RuntimeError("secret internal detail")

    service = IntakeService(database, Exploding(), today_provider=lambda: TODAY)
    result = service.process_upload("invoice.pdf", make_pdf())
    assert result.error_code == "extraction_failed"
    assert "secret" not in repr(result)
    assert database.get_attempt_for_source(result.source_document_id).status is AttemptStatus.ERROR


def test_security_rejected_upload_creates_nothing(intake, database):
    for name, content in [
        ("a.pdf", b""),
        ("a.txt", make_pdf()),
        ("a.pdf", malformed_pdf()),
        ("a.pdf", encrypted_pdf()),
    ]:
        result = intake.process_upload(name, content)
        assert result.outcome is IntakeOutcome.REJECTED
        assert result.rejection_code
        assert result.candidate is None
    snapshot = database.metrics_snapshot()
    assert snapshot["sources"] == 0
    assert snapshot["attempts"] == 0


def test_content_duplicate_returns_existing_candidate_and_counts(intake, database):
    first = intake.process_upload("invoice.pdf", make_pdf())
    second = intake.process_upload("renamed.pdf", make_pdf())
    assert second.content_duplicate
    assert second.candidate.id == first.candidate.id
    assert second.source_document_id == first.source_document_id
    assert len(database.list_candidates()) == 1
    snapshot = database.metrics_snapshot()
    assert (snapshot["sources"], snapshot["attempts"], snapshot["content_duplicates"]) == (1, 1, 1)


def test_content_duplicate_returns_candidate_in_its_current_terminal_status(
    intake, review, database
):
    first = intake.process_upload("invoice.pdf", make_pdf())
    review.approve(first.candidate.id, reviewer_label="Reviewer A")
    again = intake.process_upload("invoice.pdf", make_pdf())
    assert again.content_duplicate
    assert again.candidate.status is CandidateStatus.APPROVED


def test_content_duplicate_of_failed_source_returns_recorded_error(intake, database):
    first = intake.process_upload("scan.pdf", no_text_pdf())
    second = intake.process_upload("scan.pdf", no_text_pdf())
    assert second.content_duplicate
    assert second.outcome is IntakeOutcome.EXTRACTION_ERROR
    assert second.error_code == "no_extractable_text"
    snapshot = database.metrics_snapshot()
    assert snapshot["attempts"] == 1
    assert snapshot["content_duplicates"] == 1
    assert first.source_document_id == second.source_document_id


def test_stale_started_attempt_is_reported_as_interrupted_and_not_retried_implicitly(
    intake, database, clock
):
    pdf = make_pdf()
    source, _ = database.get_or_create_source_document(sha256_bytes(pdf), "invoice.pdf", len(pdf))
    database.create_attempt(source.id, "deterministic")
    clock.advance(seconds=STALE_ATTEMPT_AFTER.total_seconds())
    result = intake.process_upload("invoice.pdf", pdf)
    assert result.outcome is IntakeOutcome.EXTRACTION_ERROR
    assert result.error_code == "extraction_interrupted"
    assert result.retryable
    assert database.list_candidates() == []
    attempts = database.list_attempts_for_source(source.id)
    assert [a.status for a in attempts] == [AttemptStatus.STARTED]


# ------------------------------------------------------------------ explicit retry


def test_error_result_offers_retry_but_a_reupload_never_retries_by_itself(database):
    service, extractor = flaky_service(database, failures=1)
    first = service.process_upload("a.pdf", make_pdf())
    assert first.outcome is IntakeOutcome.EXTRACTION_ERROR and first.retryable
    again = service.process_upload("a.pdf", make_pdf())
    assert again.content_duplicate and again.retryable
    assert again.outcome is IntakeOutcome.EXTRACTION_ERROR
    assert extractor.calls == 1
    assert database.metrics_snapshot()["attempts"] == 1


def test_explicit_retry_after_an_error_creates_a_second_attempt_and_a_candidate(database):
    service, _ = flaky_service(database, failures=1)
    failed = service.process_upload("a.pdf", make_pdf())
    result = service.retry_extraction("a.pdf", make_pdf())
    assert result.outcome is IntakeOutcome.CANDIDATE
    assert not result.retryable
    assert result.source_document_id == failed.source_document_id
    assert result.candidate.status is CandidateStatus.AUTO_READY
    attempts = database.list_attempts_for_source(failed.source_document_id)
    assert [(a.attempt_number, a.status) for a in attempts] == [
        (1, AttemptStatus.ERROR),
        (2, AttemptStatus.SUCCEEDED),
    ]
    assert attempts[0].error_code == "extraction_failed"
    assert result.candidate.attempt_id == attempts[1].id
    assert len(database.list_candidates()) == 1


def test_retry_that_fails_again_keeps_both_failed_attempts(database):
    service, _ = flaky_service(database, failures=2, error_code="no_extractable_text")
    first = service.process_upload("a.pdf", make_pdf())
    second = service.retry_extraction("a.pdf", make_pdf())
    assert second.outcome is IntakeOutcome.EXTRACTION_ERROR
    assert second.error_code == "no_extractable_text" and second.retryable
    attempts = database.list_attempts_for_source(first.source_document_id)
    assert [a.status for a in attempts] == [AttemptStatus.ERROR, AttemptStatus.ERROR]
    third = service.retry_extraction("a.pdf", make_pdf())
    assert third.outcome is IntakeOutcome.CANDIDATE
    assert len(database.list_attempts_for_source(first.source_document_id)) == 3


def test_retry_of_a_stale_attempt_preserves_it_and_can_succeed(intake, database, clock):
    pdf = make_pdf()
    source, _ = database.get_or_create_source_document(sha256_bytes(pdf), "invoice.pdf", len(pdf))
    database.create_attempt(source.id, "deterministic")
    clock.advance(seconds=STALE_ATTEMPT_AFTER.total_seconds())
    result = intake.retry_extraction("invoice.pdf", pdf)
    assert result.outcome is IntakeOutcome.CANDIDATE
    attempts = database.list_attempts_for_source(source.id)
    assert [(a.status, a.error_code) for a in attempts] == [
        (AttemptStatus.ERROR, "extraction_interrupted"),
        (AttemptStatus.SUCCEEDED, None),
    ]


def test_retry_of_a_successful_source_returns_the_candidate_without_a_new_attempt(
    intake, database
):
    first = intake.process_upload("a.pdf", make_pdf())
    again = intake.retry_extraction("a.pdf", make_pdf())
    assert again.outcome is IntakeOutcome.CANDIDATE and again.content_duplicate
    assert again.candidate.id == first.candidate.id
    assert not again.retryable
    snapshot = database.metrics_snapshot()
    assert (snapshot["attempts"], snapshot["successful_candidates"]) == (1, 1)


def test_retry_of_an_approved_source_still_returns_the_approved_candidate(intake, review):
    first = intake.process_upload("a.pdf", make_pdf())
    review.approve(first.candidate.id, reviewer_label="Reviewer A")
    again = intake.retry_extraction("a.pdf", make_pdf())
    assert again.candidate.status is CandidateStatus.APPROVED


def test_retry_of_an_unknown_document_is_refused_and_creates_nothing(intake, database):
    result = intake.retry_extraction("a.pdf", make_pdf())
    assert result.outcome is IntakeOutcome.REJECTED
    assert result.rejection_code == "unknown_document"
    snapshot = database.metrics_snapshot()
    assert (snapshot["sources"], snapshot["attempts"]) == (0, 0)


def test_retry_still_applies_the_file_security_checks(intake, database):
    result = intake.retry_extraction("a.pdf", malformed_pdf())
    assert result.outcome is IntakeOutcome.REJECTED
    assert result.rejection_code == "unreadable_pdf"
    assert database.metrics_snapshot()["attempts"] == 0


def test_retry_does_not_count_as_a_content_duplicate_observation(database):
    service, _ = flaky_service(database, failures=1)
    service.process_upload("a.pdf", make_pdf())
    service.retry_extraction("a.pdf", make_pdf())
    snapshot = database.metrics_snapshot()
    assert snapshot["content_duplicates"] == 0
    assert (snapshot["sources"], snapshot["attempts"], snapshot["retry_attempts"]) == (1, 2, 1)


def test_retried_candidate_goes_through_duplicate_detection_and_records_initial_status(database):
    service, _ = flaky_service(database, failures=1)
    first = IntakeService(database, DeterministicExtractor(), today_provider=lambda: TODAY)
    original = first.process_upload("a.pdf", make_pdf()).candidate
    service.process_upload("b.pdf", make_pdf(extra=["Note: second copy"]))
    retried = service.retry_extraction("b.pdf", make_pdf(extra=["Note: second copy"])).candidate
    assert retried.status is CandidateStatus.REVIEW_REQUIRED
    assert retried.initial_status is CandidateStatus.REVIEW_REQUIRED
    flag = [i for i in retried.issues if i.code is IssueCode.POSSIBLE_LOGICAL_DUPLICATE]
    assert flag[0].candidate_ids == [original.id]


def test_extractor_exception_during_retry_is_recorded_generically(database):
    class FailsThenExplodes:
        def __init__(self):
            self.calls = 0

        def extract(self, pdf_bytes):
            self.calls += 1
            if self.calls == 1:
                return ExtractionResult(method="deterministic", error_code="extraction_failed")
            raise RuntimeError("secret internal detail")

    service = IntakeService(database, FailsThenExplodes(), today_provider=lambda: TODAY)
    service.process_upload("a.pdf", make_pdf())
    result = service.retry_extraction("a.pdf", make_pdf())
    assert result.error_code == "extraction_failed"
    assert "secret" not in repr(result)
    assert database.metrics_snapshot()["attempts"] == 2


def test_new_candidates_record_their_initial_routing(intake):
    clean = intake.process_upload("a.pdf", make_pdf()).candidate
    flagged = intake.process_upload(
        "b.pdf", make_pdf({"Invoice Number": "INV-DEMO-0002", "Subtotal": "1,0000"})
    ).candidate
    assert clean.initial_status is CandidateStatus.AUTO_READY
    assert flagged.initial_status is CandidateStatus.REVIEW_REQUIRED


def test_different_bytes_with_same_logical_invoice_get_blocking_duplicate_issue(intake):
    first = intake.process_upload("a.pdf", make_pdf()).candidate
    second = intake.process_upload("b.pdf", make_pdf(extra=["Note: second copy"])).candidate
    assert second.status is CandidateStatus.REVIEW_REQUIRED
    flag = [i for i in second.issues if i.code is IssueCode.POSSIBLE_LOGICAL_DUPLICATE]
    assert flag[0].candidate_ids == [first.id]


def test_future_date_routing_uses_the_injected_today(database):
    pdf = make_pdf({"Invoice Date": "2026-10-11"})
    early = IntakeService(database, DeterministicExtractor(), today_provider=lambda: TODAY)
    assert early.process_upload("a.pdf", pdf).candidate.status is CandidateStatus.REVIEW_REQUIRED
    later = IntakeService(
        database, DeterministicExtractor(), today_provider=lambda: date(2026, 10, 11)
    )
    other = make_pdf({"Invoice Date": "2026-10-11", "Invoice Number": "INV-DEMO-0002"})
    assert later.process_upload("b.pdf", other).candidate.status is CandidateStatus.AUTO_READY


def test_pdf_bytes_and_free_text_are_never_persisted(intake, db_path):
    intake.process_upload("invoice.pdf", make_pdf(extra=["Free note: do-not-store-this-text"]))
    stored = db_path.read_bytes()
    assert b"%PDF-" not in stored
    assert b"do-not-store-this-text" not in stored


# ------------------------------------------------ atomic success and recovery of orphaned success


def test_a_failed_candidate_insert_leaves_no_succeeded_attempt_and_can_be_retried(
    database, monkeypatch
):
    service = IntakeService(database, DeterministicExtractor(), today_provider=lambda: TODAY)

    def explode(*args, **kwargs):
        raise RuntimeError("injected failure with secret detail")

    with monkeypatch.context() as patch:
        patch.setattr(database, "_insert_candidate", explode)
        result = service.process_upload("a.pdf", make_pdf())
    assert result.outcome is IntakeOutcome.EXTRACTION_ERROR
    assert result.error_code == "candidate_creation_failed" and result.retryable
    assert "secret" not in repr(result)
    attempts = database.list_attempts_for_source(result.source_document_id)
    assert [(a.status, a.error_code, a.raw) for a in attempts] == [
        (AttemptStatus.ERROR, "candidate_creation_failed", None)
    ]
    assert database.list_candidates() == []

    retried = service.retry_extraction("a.pdf", make_pdf())
    assert retried.outcome is IntakeOutcome.CANDIDATE
    assert [a.status for a in database.list_attempts_for_source(result.source_document_id)] == [
        AttemptStatus.ERROR,
        AttemptStatus.SUCCEEDED,
    ]


def strand_succeeded_attempt(database, pdf):
    """The pre-fix crash window: a succeeded attempt with stored evidence but no candidate."""
    extraction = DeterministicExtractor().extract(pdf)
    source, _ = database.get_or_create_source_document(sha256_bytes(pdf), "a.pdf", len(pdf))
    attempt = database.create_attempt(source.id, "deterministic")
    with database.connect() as connection:
        connection.execute(
            "UPDATE extraction_attempts SET status = 'succeeded', raw_json = ?, "
            "field_confidence_json = ?, conflicts_json = '[]', finished_at = ? WHERE id = ?",
            (
                json.dumps(extraction.raw.to_json_dict()),
                json.dumps(extraction.field_confidence_bps),
                FIXED_NOW.isoformat(),
                attempt.id,
            ),
        )
    return source.id, database.get_attempt_for_source(source.id)


def test_reupload_recovers_a_succeeded_attempt_without_a_candidate_from_stored_evidence(database):
    source_id, attempt = strand_succeeded_attempt(database, make_pdf())
    assert database.get_candidate_for_attempt(attempt.id) is None
    extractor_calls = []

    class Spy:
        def extract(self, pdf_bytes):
            extractor_calls.append(1)
            raise AssertionError("recovery must not re-extract the PDF")

    service = IntakeService(database, Spy(), today_provider=lambda: TODAY)
    result = service.process_upload("a.pdf", make_pdf())
    assert result.outcome is IntakeOutcome.CANDIDATE and result.content_duplicate
    assert not result.retryable
    assert result.candidate.attempt_id == attempt.id
    assert result.candidate.original_raw == attempt.raw
    assert result.candidate.original_field_confidence_bps == attempt.field_confidence_bps
    assert result.candidate.initial_status is result.candidate.status
    assert extractor_calls == []
    assert len(database.list_attempts_for_source(source_id)) == 1
    again = service.process_upload("a.pdf", make_pdf())
    assert again.candidate.id == result.candidate.id
    assert len(database.list_candidates()) == 1


def test_retry_recovers_a_succeeded_attempt_without_a_candidate_instead_of_refusing(database):
    _, attempt = strand_succeeded_attempt(database, make_pdf())
    service = IntakeService(database, DeterministicExtractor(), today_provider=lambda: TODAY)
    result = service.retry_extraction("a.pdf", make_pdf())
    assert result.outcome is IntakeOutcome.CANDIDATE
    assert result.candidate.attempt_id == attempt.id
    assert database.metrics_snapshot()["attempts"] == 1


def test_recovered_candidate_goes_through_validation_and_duplicate_detection(database):
    service = IntakeService(database, DeterministicExtractor(), today_provider=lambda: TODAY)
    original = service.process_upload("first.pdf", make_pdf()).candidate
    _, attempt = strand_succeeded_attempt(database, make_pdf(extra=["Note: second copy"]))
    recovered = service.retry_extraction("b.pdf", make_pdf(extra=["Note: second copy"])).candidate
    assert recovered.status is CandidateStatus.REVIEW_REQUIRED
    flag = [i for i in recovered.issues if i.code is IssueCode.POSSIBLE_LOGICAL_DUPLICATE]
    assert flag[0].candidate_ids == [original.id]


# --------------------------------------------------- an active attempt is never interrupted


def test_reupload_while_an_attempt_is_running_reports_in_progress_and_changes_nothing(
    intake, database, clock
):
    pdf = make_pdf()
    source, _ = database.get_or_create_source_document(sha256_bytes(pdf), "invoice.pdf", len(pdf))
    running = database.create_attempt(source.id, "deterministic")
    clock.advance(seconds=STALE_ATTEMPT_AFTER.total_seconds() - 1)
    result = intake.process_upload("invoice.pdf", pdf)
    assert result.outcome is IntakeOutcome.IN_PROGRESS
    assert result.error_code == "attempt_in_progress" and not result.retryable
    assert result.candidate is None
    attempts = database.list_attempts_for_source(source.id)
    assert [(a.id, a.status) for a in attempts] == [(running.id, AttemptStatus.STARTED)]


def test_retry_while_an_attempt_is_running_reports_in_progress_and_changes_nothing(
    intake, database, clock
):
    pdf = make_pdf()
    source, _ = database.get_or_create_source_document(sha256_bytes(pdf), "invoice.pdf", len(pdf))
    running = database.create_attempt(source.id, "deterministic")
    result = intake.retry_extraction("invoice.pdf", pdf)
    assert result.outcome is IntakeOutcome.IN_PROGRESS and not result.retryable
    attempts = database.list_attempts_for_source(source.id)
    assert [(a.id, a.status, a.error_code) for a in attempts] == [
        (running.id, AttemptStatus.STARTED, None)
    ]


def test_two_sessions_retrying_at_once_do_not_interrupt_each_other(db_path, clock):
    entered, release = threading.Event(), threading.Event()
    calls = {"a": 0, "b": 0}

    class SessionAExtractor:
        """Fails once, then blocks inside the second extraction until released."""

        method = "deterministic"

        def __init__(self):
            self._real = DeterministicExtractor()

        def extract(self, pdf_bytes):
            calls["a"] += 1
            if calls["a"] == 1:
                return ExtractionResult(method=self.method, error_code="extraction_failed")
            entered.set()
            assert release.wait(timeout=20)
            return self._real.extract(pdf_bytes)

    class SessionBExtractor:
        method = "deterministic"

        def extract(self, pdf_bytes):
            calls["b"] += 1
            raise AssertionError("session B must not start an extraction")

    db_a = Database(now_provider=clock)
    db_a.initialize()
    db_b = Database(now_provider=clock)
    session_a = IntakeService(db_a, SessionAExtractor(), today_provider=lambda: TODAY)
    session_b = IntakeService(db_b, SessionBExtractor(), today_provider=lambda: TODAY)

    pdf = make_pdf()
    first = session_a.process_upload("a.pdf", pdf)
    assert first.outcome is IntakeOutcome.EXTRACTION_ERROR

    outcome = {}
    worker = threading.Thread(
        target=lambda: outcome.setdefault("a", session_a.retry_extraction("a.pdf", pdf))
    )
    worker.start()
    try:
        assert entered.wait(timeout=20)
        blocked_retry = session_b.retry_extraction("a.pdf", pdf)
        blocked_upload = session_b.process_upload("a.pdf", pdf)
        mid_flight = db_b.list_attempts_for_source(first.source_document_id)
    finally:
        release.set()
        worker.join(timeout=20)

    assert blocked_retry.outcome is IntakeOutcome.IN_PROGRESS
    assert blocked_upload.outcome is IntakeOutcome.IN_PROGRESS
    assert [a.status for a in mid_flight] == [AttemptStatus.ERROR, AttemptStatus.STARTED]
    assert mid_flight[0].error_code == "extraction_failed"
    assert calls["b"] == 0

    assert outcome["a"].outcome is IntakeOutcome.CANDIDATE
    final = db_b.list_attempts_for_source(first.source_document_id)
    assert [(a.attempt_number, a.status) for a in final] == [
        (1, AttemptStatus.ERROR),
        (2, AttemptStatus.SUCCEEDED),
    ]
    assert len(db_b.list_candidates()) == 1
