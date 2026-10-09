import json

import pytest

from factories import make_pdf
from invoice_qa.domain.models import CandidateStatus, IssueCode, ReviewEventType
from invoice_qa.services.review import ReviewError

REVIEWER = "Reviewer A"
LOOSE_VENDOR = ({"Vendor Name": None}, ["Vendor_Name: Example Demo Loose Ltd."])


def upload(intake, overrides=None, extra=None, name="invoice.pdf"):
    return intake.process_upload(name, make_pdf(overrides, extra)).candidate


def issue_codes(candidate) -> set[IssueCode]:
    return {issue.code for issue in candidate.issues}


def error_of(callable_, *args, **kwargs) -> ReviewError:
    with pytest.raises(ReviewError) as info:
        callable_(*args, **kwargs)
    return info.value


def test_correction_updates_effective_values_and_appends_safe_event(intake, review, database):
    candidate = upload(intake, {"Subtotal": "1,0000"})
    updated = review.correct(candidate.id, {"subtotal": "1,000.00"}, reviewer_label=REVIEWER)
    assert updated.effective_raw.subtotal == "1,000.00"
    assert str(updated.effective_normalized.subtotal) == "1000.00"
    assert updated.corrected_fields == {"subtotal"}
    assert updated.status is CandidateStatus.AUTO_READY
    events = database.list_review_events(candidate.id)
    assert len(events) == 1
    assert events[0].event_type is ReviewEventType.CORRECTED
    assert events[0].reviewer_label == REVIEWER
    change = events[0].details["changes"]["subtotal"]
    assert change == {"before": "1,0000", "after": "1,000.00"}


def test_correction_that_fails_normalization_is_rejected_without_side_effects(
    intake, review, database
):
    candidate = upload(intake, {"Subtotal": "1,0000"})
    error = error_of(
        review.correct, candidate.id, {"subtotal": "1.000,00"}, reviewer_label=REVIEWER
    )
    assert error.code == "invalid_correction"
    assert error.field_errors == {"subtotal": "malformed_value"}
    assert database.get_candidate(candidate.id) == candidate
    assert database.list_review_events(candidate.id) == []


def test_clearing_required_field_is_rejected_and_optional_field_is_allowed(intake, review):
    candidate = upload(intake)
    error = error_of(
        review.correct, candidate.id, {"invoice_number": None}, reviewer_label=REVIEWER
    )
    assert error.code == "required_field_cleared"
    assert error_of(
        review.correct, candidate.id, {"invoice_number": "  "}, reviewer_label=REVIEWER
    ).code == ("required_field_cleared")
    cleared = review.correct(candidate.id, {"po_reference": None}, reviewer_label=REVIEWER)
    assert cleared.effective_raw.po_reference is None
    assert IssueCode.MISSING_PO_REFERENCE in issue_codes(cleared)
    assert cleared.status is CandidateStatus.AUTO_READY


def test_unknown_field_and_unchanged_value_are_rejected(intake, review):
    candidate = upload(intake)
    assert error_of(review.correct, candidate.id, {"bogus": "x"}, reviewer_label=REVIEWER).code == (
        "unknown_field"
    )
    assert error_of(review.correct, candidate.id, {}, reviewer_label=REVIEWER).code == "no_change"
    same = {"invoice_number": "INV-DEMO-0001"}
    assert error_of(review.correct, candidate.id, same, reviewer_label=REVIEWER).code == "no_change"


def test_original_evidence_is_unchanged_by_review_actions(intake, review, database):
    candidate = upload(intake, {"Subtotal": "1,0000"})
    review.correct(candidate.id, {"subtotal": "1,000.00"}, reviewer_label=REVIEWER)
    stored = database.get_candidate(candidate.id)
    assert stored.original_raw == candidate.original_raw
    assert stored.original_normalized == candidate.original_normalized
    assert stored.original_field_confidence_bps == candidate.original_field_confidence_bps
    assert stored.original_overall_confidence_bps == candidate.original_overall_confidence_bps


def test_uncorrected_malformed_field_keeps_its_issue_after_unrelated_correction(intake, review):
    candidate = upload(intake, {"Subtotal": "1,0000"})
    updated = review.correct(candidate.id, {"po_reference": "PO-DEMO-777"}, reviewer_label=REVIEWER)
    assert IssueCode.MALFORMED_VALUE in issue_codes(updated)
    assert updated.status is CandidateStatus.REVIEW_REQUIRED


def test_confirmation_resolves_low_confidence_warning(intake, review, database):
    candidate = upload(intake, *LOOSE_VENDOR)
    assert candidate.status is CandidateStatus.REVIEW_REQUIRED
    confirmed = review.confirm_fields(candidate.id, {"vendor_name"}, reviewer_label=REVIEWER)
    assert confirmed.confirmed_fields == {"vendor_name"}
    assert confirmed.status is CandidateStatus.AUTO_READY
    assert confirmed.original_field_confidence_bps["vendor_name"] == 7500
    event = database.list_review_events(candidate.id)[0]
    assert event.event_type is ReviewEventType.CONFIRMED
    assert event.details["fields"] == ["vendor_name"]


def test_confirming_a_blocking_issue_is_rejected(intake, review):
    candidate = upload(intake, {"Subtotal": "1,0000"})
    error = error_of(review.confirm_fields, candidate.id, {"subtotal"}, reviewer_label=REVIEWER)
    assert error.code == "not_confirmable"
    assert error_of(review.confirm_fields, candidate.id, set(), reviewer_label=REVIEWER).code == (
        "not_confirmable"
    )


def test_conflicting_field_resolves_only_after_correction(intake, review):
    candidate = upload(intake, extra=["Total Amount: 1,090.00"])
    assert error_of(
        review.confirm_fields, candidate.id, {"total_amount"}, reviewer_label=REVIEWER
    ).code == ("not_confirmable")
    fixed = review.correct(candidate.id, {"total_amount": "1,080.00"}, reviewer_label=REVIEWER)
    assert IssueCode.CONFLICTING_VALUES not in issue_codes(fixed)
    assert fixed.status is CandidateStatus.AUTO_READY


def test_correcting_an_auto_ready_candidate_reruns_validation(intake, review):
    candidate = upload(intake)
    assert candidate.status is CandidateStatus.AUTO_READY
    updated = review.correct(candidate.id, {"total_amount": "1,081.00"}, reviewer_label=REVIEWER)
    assert updated.status is CandidateStatus.REVIEW_REQUIRED
    assert IssueCode.ARITHMETIC_MISMATCH in issue_codes(updated)


def test_logical_duplicates_are_recomputed_after_correction(intake, review):
    first = upload(intake)
    second = upload(intake, {"Invoice Number": "INV-DEMO-0002"}, name="second.pdf")
    assert second.status is CandidateStatus.AUTO_READY
    flagged = review.correct(
        second.id, {"invoice_number": "INV-DEMO-0001"}, reviewer_label=REVIEWER
    )
    issue = [i for i in flagged.issues if i.code is IssueCode.POSSIBLE_LOGICAL_DUPLICATE][0]
    assert issue.candidate_ids == [first.id]
    assert flagged.status is CandidateStatus.REVIEW_REQUIRED
    cleared = review.correct(
        second.id, {"invoice_number": "INV-DEMO-0003"}, reviewer_label=REVIEWER
    )
    assert IssueCode.POSSIBLE_LOGICAL_DUPLICATE not in issue_codes(cleared)
    assert cleared.status is CandidateStatus.AUTO_READY


def make_duplicate_pair(intake):
    first = upload(intake)
    second = upload(intake, extra=["Note: second copy"], name="second.pdf")
    assert IssueCode.POSSIBLE_LOGICAL_DUPLICATE in issue_codes(second)
    return first, second


def test_possible_duplicate_requires_reject_or_confirm_distinct(intake, review, database):
    first, second = make_duplicate_pair(intake)
    assert error_of(review.approve, second.id, reviewer_label=REVIEWER).code == (
        "action_not_allowed"
    )
    confirmed = review.confirm_distinct_duplicate(
        second.id, reviewer_label=REVIEWER, reason="Different shipment"
    )
    assert confirmed.confirmed_distinct_candidate_ids == {first.id}
    assert IssueCode.POSSIBLE_LOGICAL_DUPLICATE not in issue_codes(confirmed)
    assert confirmed.status is CandidateStatus.AUTO_READY
    event = database.list_review_events(second.id)[0]
    assert event.event_type is ReviewEventType.DUPLICATE_CONFIRMED_DISTINCT
    assert event.details["compared_candidate_ids"] == [first.id]
    assert event.reason == "Different shipment"


def test_confirm_distinct_needs_reason_and_an_actual_duplicate(intake, review):
    first, second = make_duplicate_pair(intake)
    assert error_of(
        review.confirm_distinct_duplicate, second.id, reviewer_label=REVIEWER, reason=" "
    ).code == ("reason_required")
    clean = upload(intake, {"Invoice Number": "INV-DEMO-5000"}, name="third.pdf")
    assert error_of(
        review.confirm_distinct_duplicate, clean.id, reviewer_label=REVIEWER, reason="why"
    ).code == ("no_duplicate")


def test_newly_arriving_duplicate_reflags_a_confirmed_candidate(intake, review):
    first, second = make_duplicate_pair(intake)
    review.confirm_distinct_duplicate(second.id, reviewer_label=REVIEWER, reason="Distinct")
    third = upload(intake, extra=["Note: third copy"], name="third.pdf")
    flagged = [i for i in third.issues if i.code is IssueCode.POSSIBLE_LOGICAL_DUPLICATE][0]
    assert set(flagged.candidate_ids) == {first.id, second.id}
    # second previously confirmed distinct from first; the new third copy re-flags it on its next
    # revalidation because the confirmed-distinct set does not contain third.
    rechecked = review.recheck(second.id, reviewer_label=REVIEWER)
    reflag = [i for i in rechecked.issues if i.code is IssueCode.POSSIBLE_LOGICAL_DUPLICATE]
    assert reflag[0].candidate_ids == [third.id]


def test_approval_revalidates_and_is_blocked_by_a_newer_duplicate(intake, review, database):
    first = upload(intake)
    second = upload(intake, extra=["Note: second copy"], name="second.pdf")
    error = error_of(review.approve, first.id, reviewer_label=REVIEWER)
    assert error.code == "approval_blocked"
    assert error.candidate.status is CandidateStatus.REVIEW_REQUIRED
    stored = database.get_candidate(first.id)
    assert stored.status is CandidateStatus.REVIEW_REQUIRED
    assert IssueCode.POSSIBLE_LOGICAL_DUPLICATE in issue_codes(stored)
    events = database.list_review_events(first.id)
    assert [e.event_type for e in events] == [ReviewEventType.APPROVAL_BLOCKED]
    assert events[0].details == {"issue_codes": ["possible_logical_duplicate"]}
    # blocked until the newer copy is rejected, after which a recheck restores auto_ready
    review.reject(second.id, reviewer_label=REVIEWER, reason="Duplicate upload")
    restored = review.recheck(first.id, reviewer_label=REVIEWER)
    assert restored.status is CandidateStatus.AUTO_READY
    assert review.approve(first.id, reviewer_label=REVIEWER).status is CandidateStatus.APPROVED


def test_blocking_issues_prevent_approval_and_review_required_is_refused_without_change(
    intake, review, database
):
    candidate = upload(intake, {"Subtotal": "1,0000"})
    error = error_of(review.approve, candidate.id, reviewer_label=REVIEWER)
    assert error.code == "action_not_allowed"
    assert database.get_candidate(candidate.id) == candidate
    assert database.list_review_events(candidate.id) == []


def test_auto_ready_candidate_approves_and_becomes_terminal(intake, review, database):
    candidate = upload(intake)
    approved = review.approve(candidate.id, reviewer_label=REVIEWER)
    assert approved.status is CandidateStatus.APPROVED
    assert database.list_review_events(candidate.id)[-1].event_type is ReviewEventType.APPROVED


def test_rejection_requires_a_reason(intake, review):
    candidate = upload(intake)
    assert error_of(review.reject, candidate.id, reviewer_label=REVIEWER, reason="").code == (
        "reason_required"
    )
    rejected = review.reject(candidate.id, reviewer_label=REVIEWER, reason="Wrong vendor")
    assert rejected.status is CandidateStatus.REJECTED


def test_review_required_candidate_can_be_rejected(intake, review):
    candidate = upload(intake, {"Subtotal": "1,0000"})
    rejected = review.reject(candidate.id, reviewer_label=REVIEWER, reason="Unreadable")
    assert rejected.status is CandidateStatus.REJECTED


@pytest.mark.parametrize("terminal", ["approve", "reject"])
def test_terminal_candidates_reject_every_later_action(intake, review, terminal):
    candidate = upload(intake)
    if terminal == "approve":
        review.approve(candidate.id, reviewer_label=REVIEWER)
    else:
        review.reject(candidate.id, reviewer_label=REVIEWER, reason="No")
    attempts = [
        lambda: review.correct(
            candidate.id, {"po_reference": "PO-DEMO-9"}, reviewer_label=REVIEWER
        ),
        lambda: review.confirm_fields(candidate.id, {"vendor_name"}, reviewer_label=REVIEWER),
        lambda: review.confirm_distinct_duplicate(
            candidate.id, reviewer_label=REVIEWER, reason="x"
        ),
        lambda: review.approve(candidate.id, reviewer_label=REVIEWER),
        lambda: review.reject(candidate.id, reviewer_label=REVIEWER, reason="again"),
        lambda: review.recheck(candidate.id, reviewer_label=REVIEWER),
    ]
    for attempt in attempts:
        assert error_of(attempt).code == "action_not_allowed"


def test_unknown_candidate_and_blank_reviewer_are_rejected(intake, review):
    assert error_of(review.approve, "missing", reviewer_label=REVIEWER).code == "not_found"
    candidate = upload(intake)
    assert error_of(review.approve, candidate.id, reviewer_label="  ").code == "reviewer_required"


def test_full_vendor_tax_id_never_appears_in_event_payloads(intake, review, database):
    candidate = upload(intake, {"Vendor Tax ID": "DEMO-TAX-1001"})
    review.correct(candidate.id, {"vendor_tax_id": "DEMO-TAX-2002"}, reviewer_label=REVIEWER)
    review.reject(candidate.id, reviewer_label=REVIEWER, reason="test")
    payload = json.dumps([e.model_dump() for e in database.list_review_events(candidate.id)])
    assert "DEMO-TAX-1001" not in payload
    assert "DEMO-TAX-2002" not in payload
    assert "1001" in payload and "2002" in payload


def test_recheck_without_stale_state_keeps_status_and_appends_event(intake, review, database):
    candidate = upload(intake)
    rechecked = review.recheck(candidate.id, reviewer_label=REVIEWER)
    assert rechecked.status is CandidateStatus.AUTO_READY
    assert database.list_review_events(candidate.id)[0].details == {
        "recheck": True,
        "issue_codes": [],
    }
