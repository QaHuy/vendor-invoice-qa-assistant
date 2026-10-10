"""UI helpers: service wiring, generic messages and display formatting. No business rules here."""

from dataclasses import dataclass

from invoice_qa.config import default_today
from invoice_qa.domain.models import (
    ALL_FIELDS,
    CandidateStatus,
    InvoiceCandidate,
    IssueCode,
    ValidationResult,
)
from invoice_qa.domain.validation import confirmable_fields, mask_tax_id
from invoice_qa.extraction.deterministic import DeterministicExtractor
from invoice_qa.infrastructure.database import Database
from invoice_qa.services.intake import IntakeService
from invoice_qa.services.metrics import MetricsService
from invoice_qa.services.review import ReviewService

FIELD_LABELS: dict[str, str] = {
    "vendor_name": "Vendor Name",
    "vendor_tax_id": "Vendor Tax ID",
    "invoice_number": "Invoice Number",
    "invoice_date": "Invoice Date",
    "currency": "Currency",
    "subtotal": "Subtotal",
    "tax_amount": "Tax Amount",
    "total_amount": "Total Amount",
    "po_reference": "PO Reference",
}
assert set(FIELD_LABELS) == set(ALL_FIELDS)

GENERIC_UPLOAD_MESSAGE = (
    "The file could not be accepted. Upload a readable, unencrypted PDF of at most 5 MiB."
)
GENERIC_REVIEW_MESSAGE = "The action could not be completed."
_TERMINAL = {CandidateStatus.APPROVED, CandidateStatus.REJECTED}

_REVIEW_MESSAGES = {
    "not_found": "The candidate no longer exists.",
    "action_not_allowed": "This action is not allowed for the candidate's current status.",
    "no_change": "No value was changed.",
    "unknown_field": "An unknown field was submitted.",
    "invalid_correction": "A corrected value could not be applied.",
    "required_field_cleared": "A required field cannot be left empty.",
    "not_confirmable": "Only low-confidence fields can be confirmed; other issues need a fix.",
    "reason_required": "A reason is required.",
    "reviewer_required": "Enter a reviewer label first.",
    "no_duplicate": "There is no unresolved possible duplicate to confirm.",
    "approval_blocked": (
        "Approval was blocked: validation found unresolved issues. The candidate is back in review."
    ),
}
_FIELD_ERROR_MESSAGES = {
    "malformed_value": "the value is malformed",
    "unsupported_currency": "the currency is not supported",
    "missing_required_field": "a value is required",
}


@dataclass(frozen=True)
class Services:
    database: Database
    intake: IntakeService
    review: ReviewService
    metrics: MetricsService


def get_services() -> Services:
    """Build services per script run; each repository call opens its own connection."""
    database = Database()
    database.initialize()
    return Services(
        database=database,
        intake=IntakeService(database, DeterministicExtractor(), today_provider=default_today),
        review=ReviewService(database, today_provider=default_today),
        metrics=MetricsService(database),
    )


def upload_rejection_message(code: str) -> str:
    return GENERIC_UPLOAD_MESSAGE


def extraction_error_message(code: str) -> str:
    return (
        "The document could not be read for extraction. You can retry the extraction; "
        "every failed attempt stays in the record."
    )


def review_error_message(code: str) -> str:
    return _REVIEW_MESSAGES.get(code, GENERIC_REVIEW_MESSAGE)


def field_error_message(field_name: str, code: str) -> str:
    reason = _FIELD_ERROR_MESSAGES.get(code, "the value is invalid")
    return f"{FIELD_LABELS.get(field_name, field_name)}: {reason}."


def is_terminal(candidate: InvoiceCandidate) -> bool:
    return candidate.status in _TERMINAL


def confirmable_for(candidate: InvoiceCandidate) -> list[str]:
    """Fields offered for confirmation: exactly those ``confirmable_fields`` allows."""
    if is_terminal(candidate):
        return []
    result = ValidationResult(
        issues=candidate.issues, effective_field_confidence={}, effective_overall_confidence=0
    )
    return sorted(confirmable_fields(result))


def has_unresolved_duplicate(candidate: InvoiceCandidate) -> bool:
    return any(i.code is IssueCode.POSSIBLE_LOGICAL_DUPLICATE for i in candidate.issues)


def display_value(field_name: str, value: str | None, *, reveal_tax_id: bool = False) -> str:
    if value is None:
        return ""
    if field_name == "vendor_tax_id" and not reveal_tax_id:
        return mask_tax_id(value)
    return value


def summarize_candidate(candidate: InvoiceCandidate) -> dict[str, str | int]:
    """Queue row. The vendor tax ID is always masked here."""
    fields = candidate.effective_normalized.to_json_dict()
    return {
        "Candidate": candidate.id[:8],
        "Vendor": fields["vendor_name"] or "",
        "Tax ID": mask_tax_id(fields["vendor_tax_id"]),
        "Invoice": fields["invoice_number"] or "",
        "Status": candidate.status.value,
        "Issues": len(candidate.issues),
    }
