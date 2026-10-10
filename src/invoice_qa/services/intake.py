"""Upload use case: file checks, extraction attempts (retry only on request), routing."""

from collections.abc import Callable
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from invoice_qa.domain.models import (
    AttemptRecord,
    AttemptStatus,
    InvoiceCandidate,
    SourceDocumentRecord,
)
from invoice_qa.domain.normalization import normalize_invoice
from invoice_qa.domain.transitions import derive_candidate_status
from invoice_qa.domain.validation import (
    effective_confidence,
    logical_duplicate_key,
    validate_invoice,
)
from invoice_qa.extraction.base import ExtractionResult, Extractor
from invoice_qa.infrastructure.database import Database, RetryNotAllowed
from invoice_qa.infrastructure.file_security import (
    UploadRejected,
    safe_display_filename,
    sha256_bytes,
    validate_pdf_upload,
)


class IntakeOutcome(StrEnum):
    REJECTED = "rejected"
    EXTRACTION_ERROR = "extraction_error"
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

        self._db.finish_attempt_success(
            attempt.id, extraction.raw, extraction.field_confidence_bps, extraction.conflicts
        )
        normalization = normalize_invoice(extraction.raw)
        key = logical_duplicate_key(normalization.fields)
        duplicates = self._db.find_logical_duplicates(key, exclude_candidate_id="")
        _, overall = effective_confidence(extraction.field_confidence_bps, set(), set())
        validation = validate_invoice(
            normalization,
            extraction.field_confidence_bps,
            extraction.conflicts,
            set(),
            set(),
            duplicates,
            set(),
            today=self._today(),
        )
        candidate = self._db.create_candidate(
            attempt_id=attempt.id,
            original_raw=extraction.raw,
            original_normalized=normalization.fields,
            original_field_confidence_bps=extraction.field_confidence_bps,
            original_overall_confidence_bps=overall,
            conflicts=extraction.conflicts,
            issues=validation.issues,
            status=derive_candidate_status(validation),
            logical_key=key,
        )
        return IntakeResult(
            outcome=IntakeOutcome.CANDIDATE,
            source_document_id=source.id,
            display_filename=source.display_filename,
            candidate=candidate,
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
            if candidate is not None:
                return IntakeResult(outcome=IntakeOutcome.CANDIDATE, candidate=candidate, **base)
        if attempt is not None and attempt.status is AttemptStatus.ERROR:
            return IntakeResult(
                outcome=IntakeOutcome.EXTRACTION_ERROR,
                error_code=attempt.error_code,
                retryable=True,
                **base,
            )
        # A stranded ``started`` attempt (or a source that never started one) is reported as
        # interrupted; only an explicit ``retry_extraction`` starts another attempt.
        return IntakeResult(
            outcome=IntakeOutcome.EXTRACTION_ERROR,
            error_code="extraction_interrupted",
            retryable=True,
            **base,
        )
