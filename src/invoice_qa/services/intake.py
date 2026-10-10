"""Upload use case: file checks, extraction attempts (retry only on request), routing."""

from collections.abc import Callable
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from invoice_qa.domain.models import (
    AttemptRecord,
    AttemptStatus,
    CandidateStatus,
    FieldConflict,
    InvoiceCandidate,
    NormalizedInvoiceFields,
    RawInvoiceFields,
    SourceDocumentRecord,
    ValidationIssue,
)
from invoice_qa.domain.normalization import normalize_invoice
from invoice_qa.domain.transitions import derive_candidate_status
from invoice_qa.domain.validation import (
    effective_confidence,
    logical_duplicate_key,
    validate_invoice,
)
from invoice_qa.extraction.base import ExtractionResult, Extractor
from invoice_qa.infrastructure.database import (
    AttemptInProgress,
    AttemptNotActive,
    Database,
    RetryNotAllowed,
)
from invoice_qa.infrastructure.file_security import (
    UploadRejected,
    safe_display_filename,
    sha256_bytes,
    validate_pdf_upload,
)


class IntakeOutcome(StrEnum):
    REJECTED = "rejected"
    EXTRACTION_ERROR = "extraction_error"
    IN_PROGRESS = "in_progress"
    CANDIDATE = "candidate"


class IntakeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: IntakeOutcome
    content_duplicate: bool = False
    rejection_code: str | None = None
    error_code: str | None = None
    retryable: bool = False
    source_document_id: str | None = None
    display_filename: str | None = None
    candidate: InvoiceCandidate | None = None


class _Assessment(BaseModel):
    """Everything a candidate needs, derived deterministically from stored raw evidence."""

    model_config = ConfigDict(frozen=True)

    normalized: NormalizedInvoiceFields
    overall_bps: int
    issues: list[ValidationIssue]
    status: CandidateStatus
    logical_key: str | None


class IntakeService:
    def __init__(
        self,
        database: Database,
        extractor: Extractor,
        *,
        today_provider: Callable[[], date],
    ) -> None:
        self._db = database
        self._extractor = extractor
        self._today = today_provider

    @property
    def _method(self) -> str:
        return getattr(self._extractor, "method", "deterministic")

    def process_upload(self, filename: str, content: bytes) -> IntakeResult:
        """New content is extracted once; known content never triggers a new attempt."""
        try:
            validate_pdf_upload(filename, content)
        except UploadRejected as rejection:
            return IntakeResult(outcome=IntakeOutcome.REJECTED, rejection_code=rejection.code)

        display_name = safe_display_filename(filename)
        source, created = self._db.get_or_create_source_document(
            sha256_bytes(content), display_name, len(content)
        )
        if not created:
            self._db.record_duplicate_observation(source.id)
            return self._existing_result(source.id, source.display_filename)

        attempt = self._db.create_attempt(source.id, self._method)
        return self._run_attempt(source, attempt, content)

    def retry_extraction(self, filename: str, content: bytes) -> IntakeResult:
        """Explicit user action: re-extract a source whose latest attempt did not succeed.

        Content is never stored, so the caller supplies the same bytes again. Every earlier
        attempt is preserved. A source that already has a candidate just returns it.
        """
        try:
            validate_pdf_upload(filename, content)
        except UploadRejected as rejection:
            return IntakeResult(outcome=IntakeOutcome.REJECTED, rejection_code=rejection.code)

        source = self._db.get_source_document_by_sha256(sha256_bytes(content))
        if source is None:
            return IntakeResult(outcome=IntakeOutcome.REJECTED, rejection_code="unknown_document")
        try:
            attempt = self._db.retry_attempt(source.id, self._method)
        except RetryNotAllowed:
            return self._existing_result(source.id, source.display_filename)
        except AttemptInProgress:
            return self._in_progress_result(source.id, source.display_filename)
        return self._run_attempt(source, attempt, content)

    def _run_attempt(
        self, source: SourceDocumentRecord, attempt: AttemptRecord, content: bytes
    ) -> IntakeResult:
        try:
            extraction = self._extractor.extract(content)
        except Exception:
            extraction = ExtractionResult(method=self._method, error_code="extraction_failed")
        if extraction.error_code is not None or extraction.raw is None:
            error_code = extraction.error_code or "extraction_failed"
            self._db.finish_attempt_error(attempt.id, error_code)
            return IntakeResult(
                outcome=IntakeOutcome.EXTRACTION_ERROR,
                error_code=error_code,
                retryable=True,
                source_document_id=source.id,
                display_filename=source.display_filename,
            )

        try:
            assessment = self._assess(
                extraction.raw, extraction.field_confidence_bps, extraction.conflicts
            )
            # One transaction: the attempt succeeds only together with its candidate.
            candidate = self._db.complete_attempt_with_candidate(
                attempt.id,
                raw=extraction.raw,
                field_confidence_bps=extraction.field_confidence_bps,
                conflicts=extraction.conflicts,
                original_normalized=assessment.normalized,
                original_overall_confidence_bps=assessment.overall_bps,
                issues=assessment.issues,
                status=assessment.status,
                logical_key=assessment.logical_key,
            )
        except AttemptNotActive:
            # Another session already closed this attempt as interrupted.
            return self._existing_result(source.id, source.display_filename)
        except Exception:
            # The transaction rolled back, so the attempt is still ``started``; record it as a
            # failed attempt (generic code only) so the user can retry.
            try:
                self._db.finish_attempt_error(attempt.id, "candidate_creation_failed")
            except Exception:
                pass
            return IntakeResult(
                outcome=IntakeOutcome.EXTRACTION_ERROR,
                error_code="candidate_creation_failed",
                retryable=True,
                source_document_id=source.id,
                display_filename=source.display_filename,
            )
        return IntakeResult(
            outcome=IntakeOutcome.CANDIDATE,
            source_document_id=source.id,
            display_filename=source.display_filename,
            candidate=candidate,
        )

    def _assess(
        self,
        raw: RawInvoiceFields,
        field_confidence_bps: dict[str, int],
        conflicts: list[FieldConflict],
    ) -> _Assessment:
        normalization = normalize_invoice(raw)
        key = logical_duplicate_key(normalization.fields)
        duplicates = self._db.find_logical_duplicates(key, exclude_candidate_id="")
        _, overall = effective_confidence(field_confidence_bps, set(), set())
        validation = validate_invoice(
            normalization,
            field_confidence_bps,
            conflicts,
            set(),
            set(),
            duplicates,
            set(),
            today=self._today(),
        )
        return _Assessment(
            normalized=normalization.fields,
            overall_bps=overall,
            issues=validation.issues,
            status=derive_candidate_status(validation),
            logical_key=key,
        )

    def _recover_candidate(self, attempt: AttemptRecord) -> InvoiceCandidate | None:
        """Rebuild the candidate of a succeeded attempt from its stored raw evidence.

        Needs no PDF and runs no extractor. Returns ``None`` if the evidence is unusable.
        """
        if attempt.raw is None:
            return None
        try:
            assessment = self._assess(attempt.raw, attempt.field_confidence_bps, attempt.conflicts)
            return self._db.attach_candidate_to_succeeded_attempt(
                attempt.id,
                original_normalized=assessment.normalized,
                original_overall_confidence_bps=assessment.overall_bps,
                issues=assessment.issues,
                status=assessment.status,
                logical_key=assessment.logical_key,
            )
        except Exception:
            return None

    def _in_progress_result(self, source_id: str, display_filename: str) -> IntakeResult:
        return IntakeResult(
            outcome=IntakeOutcome.IN_PROGRESS,
            error_code="attempt_in_progress",
            content_duplicate=True,
            source_document_id=source_id,
            display_filename=display_filename,
        )

    def _existing_result(self, source_id: str, display_filename: str) -> IntakeResult:
        attempt = self._db.get_attempt_for_source(source_id)
        base = {
            "content_duplicate": True,
            "source_document_id": source_id,
            "display_filename": display_filename,
        }
        if attempt is not None and attempt.status is AttemptStatus.SUCCEEDED:
            candidate = self._db.get_candidate_for_attempt(attempt.id)
            if candidate is None:
                candidate = self._recover_candidate(attempt)
            if candidate is not None:
                return IntakeResult(outcome=IntakeOutcome.CANDIDATE, candidate=candidate, **base)
            return IntakeResult(
                outcome=IntakeOutcome.EXTRACTION_ERROR,
                error_code="candidate_creation_failed",
                retryable=True,
                **base,
            )
        if attempt is not None and attempt.status is AttemptStatus.STARTED:
            if not self._db.is_attempt_stale(attempt):
                return self._in_progress_result(source_id, display_filename)
        if attempt is not None and attempt.status is AttemptStatus.ERROR:
            return IntakeResult(
                outcome=IntakeOutcome.EXTRACTION_ERROR,
                error_code=attempt.error_code,
                retryable=True,
                **base,
            )
        # A stale ``started`` attempt (or a source that never started one) is reported as
        # interrupted; only an explicit ``retry_extraction`` starts another attempt.
        return IntakeResult(
            outcome=IntakeOutcome.EXTRACTION_ERROR,
            error_code="extraction_interrupted",
            retryable=True,
            **base,
        )
