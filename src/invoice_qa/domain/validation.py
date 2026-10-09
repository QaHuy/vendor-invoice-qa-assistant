"""Deterministic validation; issues clear only by correction or allowed confirmation."""

from datetime import date

from invoice_qa.domain.models import (
    LOW_FIELD_CONFIDENCE_BPS,
    LOW_OVERALL_CONFIDENCE_BPS,
    MONEY_FIELDS,
    OPTIONAL_FIELDS,
    REQUIRED_FIELDS,
    FieldConflict,
    IssueCode,
    NormalizationResult,
    NormalizedInvoiceFields,
    Severity,
    ValidationIssue,
    ValidationResult,
)

_RESOLUTION = {
    IssueCode.MISSING_REQUIRED_FIELD: "Reviewer supplies a valid value.",
    IssueCode.MALFORMED_VALUE: "Reviewer supplies a valid value, or removes an optional value.",
    IssueCode.UNSUPPORTED_CURRENCY: "Reviewer supplies a supported value.",
    IssueCode.FUTURE_INVOICE_DATE: "Reviewer corrects it to a non-future date.",
    IssueCode.INVALID_AMOUNT_SIGN: "Reviewer corrects the amount.",
    IssueCode.ARITHMETIC_MISMATCH: "Reviewer corrects one or more amounts.",
    IssueCode.CONFLICTING_VALUES: "Reviewer corrects the field to the intended value.",
    IssueCode.POSSIBLE_LOGICAL_DUPLICATE: "Reviewer rejects or confirms distinctness.",
    IssueCode.LOW_FIELD_CONFIDENCE: "Reviewer corrects or confirms the field.",
    IssueCode.LOW_OVERALL_CONFIDENCE: "Recomputed from effective confidence after each action.",
    IssueCode.MISSING_PO_REFERENCE: "Approval remains allowed.",
}

_MESSAGE = {
    IssueCode.MISSING_REQUIRED_FIELD: "A required field is missing.",
    IssueCode.MALFORMED_VALUE: "The value is malformed.",
    IssueCode.UNSUPPORTED_CURRENCY: "The currency is not supported.",
    IssueCode.FUTURE_INVOICE_DATE: "The invoice date is in the future.",
    IssueCode.INVALID_AMOUNT_SIGN: "The amount has an invalid sign.",
    IssueCode.ARITHMETIC_MISMATCH: "Subtotal plus tax does not equal the total.",
    IssueCode.CONFLICTING_VALUES: "The document contains conflicting values for this field.",
    IssueCode.POSSIBLE_LOGICAL_DUPLICATE: "Another candidate may describe the same invoice.",
    IssueCode.LOW_FIELD_CONFIDENCE: "Extraction confidence for this field is low.",
    IssueCode.LOW_OVERALL_CONFIDENCE: "Overall extraction confidence is low.",
    IssueCode.MISSING_PO_REFERENCE: "No purchase-order reference was found.",
}

_SEVERITY = {
    IssueCode.LOW_FIELD_CONFIDENCE: Severity.REVIEW_WARNING,
    IssueCode.LOW_OVERALL_CONFIDENCE: Severity.REVIEW_WARNING,
    IssueCode.MISSING_PO_REFERENCE: Severity.ADVISORY,
}


def _issue(
    code: IssueCode, field_name: str | None, candidate_ids: list[str] | None = None
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        field_name=field_name,
        severity=_SEVERITY.get(code, Severity.BLOCKING),
        resolution=_RESOLUTION[code],
        message=_MESSAGE[code],
        candidate_ids=candidate_ids or [],
    )


def effective_confidence(
    original_field_confidence_bps: dict[str, int],
    corrected_fields: set[str],
    confirmed_fields: set[str],
) -> tuple[dict[str, int], int]:
    """Derive effective confidence. The original mapping is never modified."""
    touched = corrected_fields | confirmed_fields
    fields = {name: original_field_confidence_bps.get(name, 0) for name in REQUIRED_FIELDS}
    for name, value in original_field_confidence_bps.items():
        fields.setdefault(name, value)
    for name in touched:
        if name in fields:
            fields[name] = 10000
    overall = sum(fields[name] for name in REQUIRED_FIELDS) // len(REQUIRED_FIELDS)
    return fields, overall


def validate_invoice(
    normalization: NormalizationResult,
    original_field_confidence_bps: dict[str, int],
    conflicts: list[FieldConflict],
    corrected_fields: set[str],
    confirmed_fields: set[str],
    possible_duplicate_candidate_ids: list[str],
    confirmed_distinct_candidate_ids: set[str],
    *,
    today: date,
) -> ValidationResult:
    fields = normalization.fields
    errors = normalization.field_errors
    effective, overall = effective_confidence(
        original_field_confidence_bps, corrected_fields, confirmed_fields
    )
    conflicting = {c.field_name for c in conflicts}
    issues: list[ValidationIssue] = []
    blocked: set[str] = set()

    for name in REQUIRED_FIELDS + OPTIONAL_FIELDS:
        value = getattr(fields, name)
        required = name in REQUIRED_FIELDS
        code: IssueCode | None = None
        if name in conflicting and name not in corrected_fields:
            code = IssueCode.CONFLICTING_VALUES
        elif value is None and name not in errors:
            if required:
                code = IssueCode.MISSING_REQUIRED_FIELD
            elif name == "po_reference":
                code = IssueCode.MISSING_PO_REFERENCE
        elif name in errors:
            code = errors[name]
        elif name == "invoice_date" and value > today:
            code = IssueCode.FUTURE_INVOICE_DATE
        elif name in ("subtotal", "tax_amount") and value < 0:
            code = IssueCode.INVALID_AMOUNT_SIGN
        elif name == "total_amount" and value <= 0:
            code = IssueCode.INVALID_AMOUNT_SIGN
        elif required and effective[name] < LOW_FIELD_CONFIDENCE_BPS:
            code = IssueCode.LOW_FIELD_CONFIDENCE
        if code is not None:
            issue = _issue(code, name)
            issues.append(issue)
            if issue.severity is Severity.BLOCKING:
                blocked.add(name)

    arithmetic_inputs = ("currency", *MONEY_FIELDS)
    if not blocked.intersection(arithmetic_inputs) and fields.subtotal + fields.tax_amount != (
        fields.total_amount
    ):
        issues.append(_issue(IssueCode.ARITHMETIC_MISMATCH, "total_amount"))

    unresolved = sorted(set(possible_duplicate_candidate_ids) - confirmed_distinct_candidate_ids)
    if unresolved:
        issues.append(_issue(IssueCode.POSSIBLE_LOGICAL_DUPLICATE, None, unresolved))

    if overall < LOW_OVERALL_CONFIDENCE_BPS:
        issues.append(_issue(IssueCode.LOW_OVERALL_CONFIDENCE, None))

    return ValidationResult(
        issues=issues,
        effective_field_confidence=effective,
        effective_overall_confidence=overall,
    )


def confirmable_fields(result: ValidationResult) -> set[str]:
    """Fields whose only issue is a low-confidence warning (each field has at most one issue)."""
    return {
        issue.field_name
        for issue in result.issues
        if issue.code is IssueCode.LOW_FIELD_CONFIDENCE and issue.field_name
    }


def mask_tax_id(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


def logical_duplicate_key(fields: NormalizedInvoiceFields) -> str | None:
    """Key for logical-duplicate matching, or None until every needed component is valid."""
    if fields.invoice_number is None:
        return None
    if fields.vendor_tax_id is not None:
        return f"tax|{fields.vendor_tax_id}|{fields.invoice_number}"
    if fields.vendor_name is None or fields.invoice_date is None:
        return None
    name = fields.vendor_name.casefold()
    return f"name|{name}|{fields.invoice_number}|{fields.invoice_date.isoformat()}"
