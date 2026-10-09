"""Candidate status routing and allowed actions; approved and rejected are terminal."""

from invoice_qa.domain.models import (
    CandidateStatus,
    ReviewAction,
    Severity,
    ValidationResult,
)

_ALLOWED: dict[CandidateStatus, frozenset[ReviewAction]] = {
    CandidateStatus.AUTO_READY: frozenset(ReviewAction),
    CandidateStatus.REVIEW_REQUIRED: frozenset(ReviewAction) - {ReviewAction.APPROVE},
    CandidateStatus.APPROVED: frozenset(),
    CandidateStatus.REJECTED: frozenset(),
}


def derive_candidate_status(result: ValidationResult) -> CandidateStatus:
    """Open status after validation: any blocking issue or unresolved warning needs review."""
    if any(i.severity in (Severity.BLOCKING, Severity.REVIEW_WARNING) for i in result.issues):
        return CandidateStatus.REVIEW_REQUIRED
    return CandidateStatus.AUTO_READY


def allowed_transition(current: CandidateStatus, action: ReviewAction) -> bool:
    return action in _ALLOWED[current]
