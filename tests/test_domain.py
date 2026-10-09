from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from invoice_qa.domain.models import (
    OPTIONAL_FIELDS,
    REQUIRED_FIELDS,
    AttemptStatus,
    CandidateStatus,
    Currency,
    FieldConflict,
    FieldEvidence,
    IssueCode,
    NormalizedInvoiceFields,
    RawInvoiceFields,
    ReviewAction,
    Severity,
)
from invoice_qa.domain.normalization import normalize_invoice
from invoice_qa.domain.transitions import allowed_transition, derive_candidate_status
from invoice_qa.domain.validation import (
    confirmable_fields,
    effective_confidence,
    mask_tax_id,
    validate_invoice,
)

TODAY = date(2026, 10, 10)

CLEAN = {
    "vendor_name": "Example Demo Office Supplies Ltd.",
    "vendor_tax_id": "DEMO-TAX-1001",
    "invoice_number": "INV-DEMO-0001",
    "invoice_date": "2026-09-30",
    "currency": "USD",
    "subtotal": "1,000.00",
    "tax_amount": "80.00",
    "total_amount": "1,080.00",
    "po_reference": "PO-DEMO-001",
}


def raw(**overrides) -> RawInvoiceFields:
    return RawInvoiceFields(**{**CLEAN, **overrides})


def full_confidence(**overrides) -> dict[str, int]:
    base = {name: 10000 for name in REQUIRED_FIELDS}
    base.update(overrides)
    return base


def validate(
    raw_fields: RawInvoiceFields | None = None,
    confidence: dict[str, int] | None = None,
    conflicts: list[FieldConflict] | None = None,
    corrected: set[str] | None = None,
    confirmed: set[str] | None = None,
    duplicates: list[str] | None = None,
    distinct: set[str] | None = None,
    today: date = TODAY,
):
    return validate_invoice(
        normalize_invoice(raw_fields or raw()),
        confidence or full_confidence(),
        conflicts or [],
        corrected or set(),
        confirmed or set(),
        duplicates or [],
        distinct or set(),
        today=today,
    )


def codes(result) -> set[IssueCode]:
    return {issue.code for issue in result.issues}


def issue_for(result, field_name):
    matches = [i for i in result.issues if i.field_name == field_name]
    assert len(matches) <= 1, "a field must emit at most one field-level issue"
    return matches[0] if matches else None


# ---------------------------------------------------------------- normalization


def test_canonical_values_normalize_exactly():
    result = normalize_invoice(raw())
    fields = result.fields
    assert result.field_errors == {}
    assert fields.subtotal == Decimal("1000.00")
    assert fields.tax_amount == Decimal("80.00")
    assert fields.total_amount == Decimal("1080.00")
    assert fields.invoice_date == date(2026, 9, 30)
    assert fields.currency is Currency.USD
    assert fields.vendor_tax_id == "DEMO-TAX-1001"


def test_malformed_amount_stays_raw_and_maps_to_malformed_value():
    source = raw(subtotal="12O.00")
    result = normalize_invoice(source)
    assert source.subtotal == "12O.00"
    assert result.fields.subtotal is None
    assert result.field_errors == {"subtotal": IssueCode.MALFORMED_VALUE}


def test_unsupported_currency_stays_raw_and_maps_to_unsupported_currency():
    source = raw(currency="JPY")
    result = normalize_invoice(source)
    assert source.currency == "JPY"
    assert result.fields.currency is None
    assert result.field_errors["currency"] is IssueCode.UNSUPPORTED_CURRENCY


def test_lowercase_currency_is_unsupported():
    assert normalize_invoice(raw(currency="usd")).field_errors == {
        "currency": IssueCode.UNSUPPORTED_CURRENCY
    }


def test_missing_field_has_no_field_error():
    result = normalize_invoice(raw(invoice_number=None, subtotal=None))
    assert result.fields.invoice_number is None
    assert result.fields.subtotal is None
    assert result.field_errors == {}


@pytest.mark.parametrize("value", ["1,00,000", "1,0000", "1.000,00", ".50", "1,000."])
def test_locale_and_shape_variants_are_malformed(value):
    result = normalize_invoice(raw(subtotal=value))
    assert result.fields.subtotal is None
    assert result.field_errors == {"subtotal": IssueCode.MALFORMED_VALUE}


def test_non_ascii_digits_are_malformed():
    result = normalize_invoice(raw(subtotal="١٠٠.٠٠"))
    assert result.field_errors == {"subtotal": IssueCode.MALFORMED_VALUE}


@pytest.mark.parametrize(
    ("currency", "value"),
    [("VND", "1,000.50"), ("USD", "10.005"), ("VND", "1000.0")],
)
def test_excess_fractional_digits_are_malformed_never_rounded(currency, value):
    result = normalize_invoice(
        raw(currency=currency, subtotal=value, tax_amount="0", total_amount="1")
    )
    assert result.fields.subtotal is None
    assert result.field_errors == {"subtotal": IssueCode.MALFORMED_VALUE}


def test_fewer_digits_are_padded_exactly():
    result = normalize_invoice(raw(tax_amount="80.5"))
    assert result.fields.tax_amount == Decimal("80.50")
    assert str(result.fields.tax_amount) == "80.50"


def test_integer_usd_amount_is_padded_and_vnd_stays_integral():
    assert str(normalize_invoice(raw(tax_amount="80")).fields.tax_amount) == "80.00"
    vnd = normalize_invoice(
        raw(currency="VND", subtotal="1,000", tax_amount="0", total_amount="1,000")
    )
    assert vnd.field_errors == {}
    assert str(vnd.fields.subtotal) == "1000"


def test_invalid_currency_skips_precision_check():
    result = normalize_invoice(raw(currency="JPY", subtotal="10.005"))
    assert result.field_errors == {"currency": IssueCode.UNSUPPORTED_CURRENCY}
    assert result.fields.subtotal == Decimal("10.005")


def test_missing_currency_skips_precision_check():
    result = normalize_invoice(raw(currency=None, subtotal="10.005"))
    assert result.field_errors == {}
    assert result.fields.subtotal == Decimal("10.005")


def test_invalid_currency_still_pattern_checks_amounts():
    result = normalize_invoice(raw(currency="JPY", subtotal="1,0000"))
    assert result.field_errors["subtotal"] is IssueCode.MALFORMED_VALUE


def test_decimal_and_date_round_trip_through_canonical_strings():
    fields = normalize_invoice(raw(tax_amount="80.5")).fields
    payload = fields.to_json_dict()
    assert payload["tax_amount"] == "80.50"
    assert payload["invoice_date"] == "2026-09-30"
    assert payload["currency"] == "USD"
    assert NormalizedInvoiceFields.from_json_dict(payload) == fields


def test_decimal_serialization_has_no_exponent():
    fields = NormalizedInvoiceFields(total_amount=Decimal("1E+3"))
    assert fields.to_json_dict()["total_amount"] == "1000"


def test_iso_date_parses_and_ambiguous_date_is_malformed():
    assert normalize_invoice(raw(invoice_date="2026-01-02")).fields.invoice_date == date(2026, 1, 2)
    for value in ("02/01/2026", "2026-1-2", "20260102", "2026-13-01", "2026-02-30"):
        result = normalize_invoice(raw(invoice_date=value))
        assert result.fields.invoice_date is None
        assert result.field_errors == {"invoice_date": IssueCode.MALFORMED_VALUE}


def test_tax_id_is_trimmed_uppercased_and_pattern_checked():
    lowered = normalize_invoice(raw(vendor_tax_id=" demo-tax-1001 "))
    assert lowered.fields.vendor_tax_id == "DEMO-TAX-1001"
    result = normalize_invoice(raw(vendor_tax_id="TAX-1001"))
    assert result.fields.vendor_tax_id is None
    assert result.field_errors == {"vendor_tax_id": IssueCode.MALFORMED_VALUE}


def test_names_and_identifiers_normalize_deterministically():
    result = normalize_invoice(
        raw(
            vendor_name="  Example   Demo \t Supplies  ",
            invoice_number="  INV-DEMO-0001 ",
            po_reference=" PO-DEMO-001  ",
        )
    )
    assert result.fields.vendor_name == "Example Demo Supplies"
    assert result.fields.invoice_number == "INV-DEMO-0001"
    assert result.fields.po_reference == "PO-DEMO-001"


# ----------------------------------------------------------------- confidence


def test_confidence_model_rejects_out_of_range_instead_of_clamping():
    with pytest.raises(ValidationError):
        FieldEvidence(field_name="vendor_name", raw_value="x", confidence_bps=10001)
    with pytest.raises(ValidationError):
        FieldEvidence(field_name="vendor_name", raw_value="x", confidence_bps=-1)


def test_effective_confidence_counts_corrected_and_confirmed_without_mutating_original():
    original = full_confidence(vendor_name=7500, invoice_number=0)
    snapshot = dict(original)
    fields, overall = effective_confidence(original, {"invoice_number"}, {"vendor_name"})
    assert fields["vendor_name"] == 10000
    assert fields["invoice_number"] == 10000
    assert overall == 10000
    assert original == snapshot


@pytest.mark.parametrize(
    ("overrides", "overall", "warns"),
    [
        ({"vendor_name": 7500}, 9642, False),
        ({"invoice_number": 0}, 8571, False),
        ({"invoice_number": 0, "vendor_name": 9000}, 8428, True),
    ],
)
def test_worked_confidence_examples(overrides, overall, warns):
    confidence = full_confidence(**overrides)
    _, computed = effective_confidence(confidence, set(), set())
    assert computed == overall
    result = validate(confidence=confidence)
    assert (IssueCode.LOW_OVERALL_CONFIDENCE in codes(result)) is warns


def test_missing_and_conflicting_fields_contribute_zero():
    _, overall = effective_confidence(full_confidence(vendor_name=0, subtotal=0), set(), set())
    assert overall == (0 + 10000 + 10000 + 10000 + 0 + 10000 + 10000) // 7


# ----------------------------------------------------------------- validation


def test_clean_invoice_has_no_blocking_issue_or_warning():
    result = validate()
    assert result.issues == []
    assert result.effective_overall_confidence == 10000


def test_missing_required_field_emits_only_missing_required_field():
    result = validate(raw(invoice_number=None), full_confidence(invoice_number=0))
    issue = issue_for(result, "invoice_number")
    assert issue.code is IssueCode.MISSING_REQUIRED_FIELD
    assert issue.severity is Severity.BLOCKING


def test_conflicting_field_resolves_only_by_correction():
    conflict = FieldConflict(field_name="total_amount", values=["1,080.00", "1,090.00"])
    source = raw(total_amount=None)
    confidence = full_confidence(total_amount=0)
    unresolved = validate(source, confidence, [conflict])
    assert issue_for(unresolved, "total_amount").code is IssueCode.CONFLICTING_VALUES
    confirmed_only = validate(source, confidence, [conflict], confirmed={"total_amount"})
    assert issue_for(confirmed_only, "total_amount").code is IssueCode.CONFLICTING_VALUES
    corrected = validate(raw(), confidence, [conflict], corrected={"total_amount"})
    assert issue_for(corrected, "total_amount") is None


def test_malformed_and_unsupported_values_are_blocking_with_exact_codes():
    result = validate(raw(subtotal="1,0000", currency="JPY"))
    assert issue_for(result, "subtotal").code is IssueCode.MALFORMED_VALUE
    assert issue_for(result, "currency").code is IssueCode.UNSUPPORTED_CURRENCY
    assert issue_for(result, "subtotal").severity is Severity.BLOCKING
    assert issue_for(result, "currency").severity is Severity.BLOCKING


def test_malformed_tax_id_is_blocking_but_missing_tax_id_is_not_an_issue():
    bad = issue_for(validate(raw(vendor_tax_id="BAD")), "vendor_tax_id")
    assert bad.code is IssueCode.MALFORMED_VALUE
    assert issue_for(validate(raw(vendor_tax_id=None)), "vendor_tax_id") is None


def test_missing_po_reference_is_advisory_only():
    result = validate(raw(po_reference=None))
    issue = issue_for(result, "po_reference")
    assert issue.code is IssueCode.MISSING_PO_REFERENCE
    assert issue.severity is Severity.ADVISORY
    assert derive_candidate_status(result) is CandidateStatus.AUTO_READY


def test_future_date_uses_injected_today_only():
    source = raw(invoice_date="2026-10-11")
    assert issue_for(validate(source, today=TODAY), "invoice_date").code is (
        IssueCode.FUTURE_INVOICE_DATE
    )
    assert issue_for(validate(source, today=date(2026, 10, 11)), "invoice_date") is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"subtotal": "-1.00", "total_amount": "79.00"},
        {"tax_amount": "-1.00", "total_amount": "999.00"},
        {"subtotal": "0.00", "tax_amount": "0.00", "total_amount": "0.00"},
    ],
)
def test_invalid_amount_sign_is_blocking(overrides):
    result = validate(raw(**overrides))
    assert IssueCode.INVALID_AMOUNT_SIGN in codes(result)
    assert IssueCode.ARITHMETIC_MISMATCH not in codes(result)


def test_arithmetic_mismatch_is_blocking():
    result = validate(raw(total_amount="1,081.00"))
    assert codes(result) == {IssueCode.ARITHMETIC_MISMATCH}
    assert result.issues[0].severity is Severity.BLOCKING


def test_arithmetic_is_skipped_when_a_money_field_has_a_blocking_issue():
    result = validate(raw(total_amount="9,9999"))
    assert codes(result) == {IssueCode.MALFORMED_VALUE}
    skipped_by_currency = validate(raw(currency="JPY", total_amount="1,081.00"))
    assert codes(skipped_by_currency) == {IssueCode.UNSUPPORTED_CURRENCY}


def test_arithmetic_still_runs_when_a_money_field_has_only_low_confidence():
    result = validate(raw(total_amount="1,081.00"), full_confidence(subtotal=7500))
    assert codes(result) == {IssueCode.LOW_FIELD_CONFIDENCE, IssueCode.ARITHMETIC_MISMATCH}


def test_low_field_confidence_is_a_review_warning_at_the_boundary():
    below = validate(confidence=full_confidence(vendor_name=7999))
    at = validate(confidence=full_confidence(vendor_name=8000))
    assert issue_for(below, "vendor_name").severity is Severity.REVIEW_WARNING
    assert issue_for(at, "vendor_name") is None


def test_overall_confidence_boundary():
    # 5 x 10000 + 7500 + 2000 = 59500 -> floor(59500 / 7) = 8500: at the limit, no warning
    at = validate(confidence=full_confidence(vendor_name=7500, invoice_number=2000))
    assert at.effective_overall_confidence == 8500
    assert IssueCode.LOW_OVERALL_CONFIDENCE not in codes(at)
    below = validate(confidence=full_confidence(vendor_name=7500, invoice_number=1999))
    assert below.effective_overall_confidence == 8499
    assert IssueCode.LOW_OVERALL_CONFIDENCE in codes(below)


def test_overall_warning_is_independent_of_field_level_issues():
    result = validate(confidence=full_confidence(vendor_name=0, invoice_number=0, currency=0))
    overall = [i for i in result.issues if i.code is IssueCode.LOW_OVERALL_CONFIDENCE]
    assert len(overall) == 1
    assert overall[0].field_name is None
    assert overall[0].severity is Severity.REVIEW_WARNING


def test_confirming_low_confidence_field_resolves_its_warning():
    confidence = full_confidence(vendor_name=7500)
    assert issue_for(validate(confidence=confidence), "vendor_name") is not None
    result = validate(confidence=confidence, confirmed={"vendor_name"})
    assert result.issues == []
    assert result.effective_field_confidence["vendor_name"] == 10000


def test_confirmation_never_resolves_a_blocking_issue():
    result = validate(raw(subtotal="1,0000"), confirmed={"subtotal"})
    assert issue_for(result, "subtotal").code is IssueCode.MALFORMED_VALUE
    assert derive_candidate_status(result) is CandidateStatus.REVIEW_REQUIRED


def test_confirmable_fields_are_only_sole_low_confidence_fields():
    result = validate(
        raw(subtotal="1,0000", invoice_number=None),
        full_confidence(vendor_name=7500, subtotal=7500, invoice_number=0),
    )
    assert confirmable_fields(result) == {"vendor_name"}


def test_duplicate_ids_confirmed_distinct_are_suppressed_and_new_ids_reflag():
    flagged = validate(duplicates=["cand-1", "cand-2"])
    issues = [i for i in flagged.issues if i.code is IssueCode.POSSIBLE_LOGICAL_DUPLICATE]
    assert len(issues) == 1 and issues[0].severity is Severity.BLOCKING
    assert validate(duplicates=["cand-1"], distinct={"cand-1"}).issues == []
    reflagged = validate(duplicates=["cand-1", "cand-3"], distinct={"cand-1"})
    assert IssueCode.POSSIBLE_LOGICAL_DUPLICATE in codes(reflagged)


def test_duplicate_issue_lists_unresolved_ids_in_sorted_order():
    result = validate(duplicates=["cand-b", "cand-a", "cand-c"], distinct={"cand-c"})
    issue = [i for i in result.issues if i.code is IssueCode.POSSIBLE_LOGICAL_DUPLICATE][0]
    assert issue.candidate_ids == ["cand-a", "cand-b"]


def test_field_priority_puts_future_date_before_low_confidence():
    result = validate(raw(invoice_date="2030-01-01"), full_confidence(invoice_date=7500))
    assert issue_for(result, "invoice_date").code is IssueCode.FUTURE_INVOICE_DATE


def test_mask_tax_id_reveals_at_most_four_trailing_characters():
    assert mask_tax_id("DEMO-TAX-1001") == "*********1001"
    assert mask_tax_id("AB") == "**"
    assert mask_tax_id("ABCD") == "****"
    assert mask_tax_id(None) == ""
    assert mask_tax_id("") == ""


def test_field_sets_are_exact():
    assert REQUIRED_FIELDS == (
        "vendor_name",
        "invoice_number",
        "invoice_date",
        "currency",
        "subtotal",
        "tax_amount",
        "total_amount",
    )
    assert OPTIONAL_FIELDS == ("vendor_tax_id", "po_reference")


# ---------------------------------------------------------------- transitions


def test_status_derivation_routes_by_issue_severity():
    assert derive_candidate_status(validate()) is CandidateStatus.AUTO_READY
    assert derive_candidate_status(validate(raw(po_reference=None))) is CandidateStatus.AUTO_READY
    low = validate(confidence=full_confidence(vendor_name=7500))
    assert derive_candidate_status(low) is CandidateStatus.REVIEW_REQUIRED
    missing = validate(raw(invoice_number=None))
    assert derive_candidate_status(missing) is CandidateStatus.REVIEW_REQUIRED


@pytest.mark.parametrize("action", list(ReviewAction))
def test_auto_ready_allows_every_action(action):
    assert allowed_transition(CandidateStatus.AUTO_READY, action)


@pytest.mark.parametrize("action", list(ReviewAction))
def test_review_required_allows_everything_except_approve(action):
    expected = action is not ReviewAction.APPROVE
    assert allowed_transition(CandidateStatus.REVIEW_REQUIRED, action) is expected


@pytest.mark.parametrize("status", [CandidateStatus.APPROVED, CandidateStatus.REJECTED])
@pytest.mark.parametrize("action", list(ReviewAction))
def test_terminal_states_reject_every_action(status, action):
    assert not allowed_transition(status, action)


def test_status_and_attempt_enums_have_documented_values():
    assert {s.value for s in CandidateStatus} == {
        "auto_ready",
        "review_required",
        "approved",
        "rejected",
    }
    assert {s.value for s in AttemptStatus} == {"started", "succeeded", "error"}
    assert {a.value for a in ReviewAction} == {
        "correct",
        "confirm",
        "confirm_distinct",
        "approve",
        "reject",
    }
    assert {c.value for c in IssueCode} == {
        "missing_required_field",
        "malformed_value",
        "unsupported_currency",
        "future_invoice_date",
        "invalid_amount_sign",
        "arithmetic_mismatch",
        "conflicting_values",
        "possible_logical_duplicate",
        "low_field_confidence",
        "low_overall_confidence",
        "missing_po_reference",
    }
