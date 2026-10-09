"""Reviewer actions. Every action revalidates from scratch and commits state plus one event."""

from collections.abc import Callable
from datetime import date
from typing import Any

from invoice_qa.domain.models import (
    ALL_FIELDS,
    REQUIRED_FIELDS,
    CandidateStatus,
    InvoiceCandidate,
    NormalizationResult,
    RawInvoiceFields,
    ReviewAction,
    ReviewEventType,
    ValidationResult,
)
from invoice_qa.domain.normalization import normalize_invoice
from invoice_qa.domain.transitions import allowed_transition, derive_candidate_status
from invoice_qa.domain.validation import (
    confirmable_fields,
    logical_duplicate_key,
    mask_tax_id,
    validate_invoice,
)
from invoice_qa.infrastructure.database import Database


class ReviewError(Exception):
    """A refused action. Carries stable codes only, never values or internal detail."""

    def __init__(
        self,
        code: str,
        *,
        field_errors: dict[str, str] | None = None,
        candidate: InvoiceCandidate | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.field_errors = field_errors or {}
        self.candidate = candidate


def _issue_codes(result: ValidationResult) -> list[str]:
    return sorted({issue.code.value for issue in result.issues})


def _safe_value(field_name: str, value: str | None) -> str | None:
    return mask_tax_id(value) if field_name == "vendor_tax_id" else value


class ReviewService:
    def __init__(self, database: Database, *, today_provider: Callable[[], date]) -> None:
        self._db = database
        self._today = today_provider

    # ----------------------------------------------------------------- actions

    def correct(
        self, candidate_id: str, changes: dict[str, str | None], *, reviewer_label: str
    ) -> InvoiceCandidate:
        candidate = self._load(candidate_id, ReviewAction.CORRECT, reviewer_label)
        if not changes:
            raise ReviewError("no_change")
        if not set(changes) <= set(ALL_FIELDS):
            raise ReviewError("unknown_field")

        current = candidate.effective_raw.model_dump()
        changed: dict[str, str | None] = {}
        for name, value in changes.items():
            cleaned = (value or "").strip() or None
            if cleaned is None and name in REQUIRED_FIELDS:
                raise ReviewError(
                    "required_field_cleared",
                    field_errors={name: "missing_required_field"},
                )
            if cleaned != current[name]:
                changed[name] = cleaned
        if not changed:
            raise ReviewError("no_change")

        new_raw = RawInvoiceFields(**{**current, **changed})
        normalization = normalize_invoice(new_raw)
        rejected = {
            name: normalization.field_errors[name].value
            for name in changed
            if name in normalization.field_errors
        }
        if rejected:
            raise ReviewError("invalid_correction", field_errors=rejected)

        corrected = candidate.corrected_fields | set(changed)
        validation, key = self._validate(
            candidate,
            normalization,
            corrected,
            candidate.confirmed_fields,
            candidate.confirmed_distinct_candidate_ids,
        )
        details = {
            "changes": {
                name: {
                    "before": _safe_value(name, current[name]),
                    "after": _safe_value(name, new_value),
                }
                for name, new_value in changed.items()
            },
            "issue_codes": _issue_codes(validation),
        }
        return self._commit(
            candidate,
            new_raw,
            normalization,
            corrected,
            candidate.confirmed_fields,
            candidate.confirmed_distinct_candidate_ids,
            validation,
            key,
            derive_candidate_status(validation),
            ReviewEventType.CORRECTED,
            reviewer_label,
            None,
            details,
        )

    def confirm_fields(
        self, candidate_id: str, field_names: set[str], *, reviewer_label: str
    ) -> InvoiceCandidate:
        candidate = self._load(candidate_id, ReviewAction.CONFIRM, reviewer_label)
        normalization = normalize_invoice(candidate.effective_raw)
        current, _ = self._validate(
            candidate,
            normalization,
            candidate.corrected_fields,
            candidate.confirmed_fields,
            candidate.confirmed_distinct_candidate_ids,
        )
        if not field_names or not field_names <= confirmable_fields(current):
            raise ReviewError("not_confirmable")
        confirmed = candidate.confirmed_fields | field_names
        validation, key = self._validate(
            candidate,
            normalization,
            candidate.corrected_fields,
            confirmed,
            candidate.confirmed_distinct_candidate_ids,
        )
        return self._commit(
            candidate,
            candidate.effective_raw,
            normalization,
            candidate.corrected_fields,
            confirmed,
            candidate.confirmed_distinct_candidate_ids,
            validation,
            key,
            derive_candidate_status(validation),
            ReviewEventType.CONFIRMED,
            reviewer_label,
            None,
            {"fields": sorted(field_names), "issue_codes": _issue_codes(validation)},
        )

    def confirm_distinct_duplicate(
        self, candidate_id: str, *, reviewer_label: str, reason: str
    ) -> InvoiceCandidate:
        candidate = self._load(candidate_id, ReviewAction.CONFIRM_DISTINCT, reviewer_label)
        if not reason.strip():
            raise ReviewError("reason_required")
        normalization = normalize_invoice(candidate.effective_raw)
        key = logical_duplicate_key(normalization.fields)
        matches = self._db.find_logical_duplicates(key, exclude_candidate_id=candidate.id)
        unresolved = set(matches) - candidate.confirmed_distinct_candidate_ids
        if not unresolved:
            raise ReviewError("no_duplicate")
        distinct = candidate.confirmed_distinct_candidate_ids | set(matches)
        validation, key = self._validate(
            candidate,
            normalization,
            candidate.corrected_fields,
            candidate.confirmed_fields,
            distinct,
        )
        return self._commit(
            candidate,
            candidate.effective_raw,
            normalization,
            candidate.corrected_fields,
            candidate.confirmed_fields,
            distinct,
            validation,
            key,
            derive_candidate_status(validation),
            ReviewEventType.DUPLICATE_CONFIRMED_DISTINCT,
            reviewer_label,
            reason.strip(),
            {"compared_candidate_ids": sorted(matches)},
        )

    def recheck(self, candidate_id: str, *, reviewer_label: str) -> InvoiceCandidate:
        """Re-run validation without changing any value (e.g. after a duplicate was rejected)."""
        candidate = self._load(candidate_id, ReviewAction.CONFIRM, reviewer_label)
        normalization = normalize_invoice(candidate.effective_raw)
        validation, key = self._validate(
            candidate,
            normalization,
            candidate.corrected_fields,
            candidate.confirmed_fields,
            candidate.confirmed_distinct_candidate_ids,
        )
        return self._commit(
            candidate,
            candidate.effective_raw,
            normalization,
            candidate.corrected_fields,
            candidate.confirmed_fields,
            candidate.confirmed_distinct_candidate_ids,
            validation,
            key,
            derive_candidate_status(validation),
            ReviewEventType.CONFIRMED,
            reviewer_label,
            None,
            {"recheck": True, "issue_codes": _issue_codes(validation)},
        )

    def approve(self, candidate_id: str, *, reviewer_label: str) -> InvoiceCandidate:
        candidate = self._load(candidate_id, ReviewAction.APPROVE, reviewer_label)
        normalization = normalize_invoice(candidate.effective_raw)
        validation, key = self._validate(
            candidate,
            normalization,
            candidate.corrected_fields,
            candidate.confirmed_fields,
            candidate.confirmed_distinct_candidate_ids,
        )
        clean = derive_candidate_status(validation) is CandidateStatus.AUTO_READY
        updated = self._commit(
            candidate,
            candidate.effective_raw,
            normalization,
            candidate.corrected_fields,
            candidate.confirmed_fields,
            candidate.confirmed_distinct_candidate_ids,
            validation,
            key,
            CandidateStatus.APPROVED if clean else CandidateStatus.REVIEW_REQUIRED,
            ReviewEventType.APPROVED if clean else ReviewEventType.APPROVAL_BLOCKED,
            reviewer_label,
            None,
            {} if clean else {"issue_codes": _issue_codes(validation)},
        )
        if not clean:
            raise ReviewError("approval_blocked", candidate=updated)
        return updated

    def reject(self, candidate_id: str, *, reviewer_label: str, reason: str) -> InvoiceCandidate:
        candidate = self._load(candidate_id, ReviewAction.REJECT, reviewer_label)
        if not reason.strip():
            raise ReviewError("reason_required")
        return self._db.apply_review_change_with_event(
            candidate.id,
            effective_raw=candidate.effective_raw,
            effective_normalized=candidate.effective_normalized,
            corrected_fields=candidate.corrected_fields,
            confirmed_fields=candidate.confirmed_fields,
            confirmed_distinct_candidate_ids=candidate.confirmed_distinct_candidate_ids,
            issues=candidate.issues,
            status=CandidateStatus.REJECTED,
            logical_key=logical_duplicate_key(candidate.effective_normalized),
            event_type=ReviewEventType.REJECTED,
            reviewer_label=reviewer_label.strip(),
            reason=reason.strip(),
            details={},
        )

    # ----------------------------------------------------------------- helpers

    def _load(
        self, candidate_id: str, action: ReviewAction, reviewer_label: str
    ) -> InvoiceCandidate:
        if not reviewer_label.strip():
            raise ReviewError("reviewer_required")
        candidate = self._db.get_candidate(candidate_id)
        if candidate is None:
            raise ReviewError("not_found")
        if not allowed_transition(candidate.status, action):
            raise ReviewError("action_not_allowed")
        return candidate

    def _validate(
        self,
        candidate: InvoiceCandidate,
        normalization: NormalizationResult,
        corrected: set[str],
        confirmed: set[str],
        distinct: set[str],
    ) -> tuple[ValidationResult, str | None]:
        key = logical_duplicate_key(normalization.fields)
        matches = self._db.find_logical_duplicates(key, exclude_candidate_id=candidate.id)
        result = validate_invoice(
            normalization,
            candidate.original_field_confidence_bps,
            candidate.conflicts,
            corrected,
            confirmed,
            matches,
            distinct,
            today=self._today(),
        )
        return result, key

    def _commit(
        self,
        candidate: InvoiceCandidate,
        raw: RawInvoiceFields,
        normalization: NormalizationResult,
        corrected: set[str],
        confirmed: set[str],
        distinct: set[str],
        validation: ValidationResult,
        key: str | None,
        status: CandidateStatus,
        event_type: ReviewEventType,
        reviewer_label: str,
        reason: str | None,
        details: dict[str, Any],
    ) -> InvoiceCandidate:
        return self._db.apply_review_change_with_event(
            candidate.id,
            effective_raw=raw,
            effective_normalized=normalization.fields,
            corrected_fields=corrected,
            confirmed_fields=confirmed,
            confirmed_distinct_candidate_ids=distinct,
            issues=validation.issues,
            status=status,
            logical_key=key,
            event_type=event_type,
            reviewer_label=reviewer_label.strip(),
            reason=reason,
            details=details,
        )
