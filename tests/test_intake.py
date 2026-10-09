from datetime import date

from conftest import TODAY
from factories import encrypted_pdf, make_pdf, malformed_pdf, no_text_pdf
from invoice_qa.domain.models import AttemptStatus, CandidateStatus, IssueCode
from invoice_qa.extraction.base import ExtractionResult
from invoice_qa.extraction.deterministic import DeterministicExtractor
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


def test_stranded_started_attempt_is_reported_as_interrupted_never_retried(intake, database):
    pdf = make_pdf()
    from invoice_qa.infrastructure.file_security import sha256_bytes

    source, _ = database.get_or_create_source_document(sha256_bytes(pdf), "invoice.pdf", len(pdf))
    database.create_attempt(source.id, "deterministic")
    result = intake.process_upload("invoice.pdf", pdf)
    assert result.outcome is IntakeOutcome.EXTRACTION_ERROR
    assert result.error_code == "extraction_interrupted"
    assert database.list_candidates() == []


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
