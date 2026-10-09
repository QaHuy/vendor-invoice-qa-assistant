"""Review Queue page: inspect, correct, confirm, resolve duplicates, approve, reject, export."""

import json
from collections.abc import Callable

import pandas as pd
import streamlit as st

from invoice_qa.domain.models import ALL_FIELDS, CandidateStatus, InvoiceCandidate
from invoice_qa.domain.validation import effective_confidence
from invoice_qa.services.export import approved_rows, rows_to_csv, rows_to_json
from invoice_qa.services.review import ReviewError
from invoice_qa.ui import helpers
from invoice_qa.ui.helpers import FIELD_LABELS, Services

_FLASH = "review_flash"


def _flash(kind: str, message: str) -> None:
    st.session_state[_FLASH] = (kind, message)
    st.rerun()


def _show_flash() -> None:
    kind, message = st.session_state.pop(_FLASH, (None, None))
    if kind:
        getattr(st, kind)(message)


def _perform(action: Callable[[], InvoiceCandidate], success: str) -> None:
    try:
        action()
    except ReviewError as error:
        if error.code == "approval_blocked":
            _flash("warning", helpers.review_error_message(error.code))
        elif error.code == "invalid_correction":
            for name, code in error.field_errors.items():
                st.error(helpers.field_error_message(name, code))
        elif error.code == "required_field_cleared":
            for name in error.field_errors:
                st.error(f"{FIELD_LABELS[name]}: a value is required.")
        else:
            st.error(helpers.review_error_message(error.code))
        return
    _flash("success", success)


def _sync_edit_fields(candidate: InvoiceCandidate) -> None:
    marker = (candidate.id, candidate.updated_at)
    if st.session_state.get("_edit_marker") != marker:
        raw = candidate.effective_raw.model_dump()
        for name in ALL_FIELDS:
            st.session_state[f"field_{name}"] = raw[name] or ""
        st.session_state["_edit_marker"] = marker


def _comparison(candidate: InvoiceCandidate, reveal: bool) -> pd.DataFrame:
    effective, _ = effective_confidence(
        candidate.original_field_confidence_bps,
        candidate.corrected_fields,
        candidate.confirmed_fields,
    )
    original_raw = candidate.original_raw.model_dump()
    original_norm = candidate.original_normalized.to_json_dict()
    current_raw = candidate.effective_raw.model_dump()
    current_norm = candidate.effective_normalized.to_json_dict()
    rows = []
    for name in ALL_FIELDS:

        def shown(value: str | None, field: str = name) -> str:
            return helpers.display_value(field, value, reveal_tax_id=reveal)

        rows.append(
            {
                "Field": FIELD_LABELS[name],
                "Raw (original)": shown(original_raw[name]),
                "Normalized (original)": shown(original_norm[name]),
                "Effective": shown(current_norm[name] or current_raw[name]),
                "Confidence (original)": candidate.original_field_confidence_bps.get(name, 0),
                "Confidence (effective)": effective.get(name, 0),
            }
        )
    return pd.DataFrame(rows)


def _timeline(services: Services, candidate: InvoiceCandidate) -> None:
    events = services.database.list_review_events(candidate.id)
    if not events:
        st.caption("No review events yet.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Time": e.created_at,
                    "Event": e.event_type.value,
                    "Reviewer": e.reviewer_label,
                    "Reason": e.reason or "",
                    "Details": json.dumps(e.details, sort_keys=True),
                }
                for e in events
            ]
        ),
        hide_index=True,
    )


def _export_section(services: Services) -> None:
    st.subheader("Export approved records")
    rows = approved_rows(services.database)
    st.caption(f"{len(rows)} approved record(s). Vendor tax IDs are masked in exports.")
    csv_col, json_col = st.columns(2)
    csv_col.download_button(
        "Download CSV",
        rows_to_csv(rows),
        file_name="approved_invoices.csv",
        mime="text/csv",
        disabled=not rows,
        key="download_csv",
    )
    json_col.download_button(
        "Download JSON",
        rows_to_json(rows),
        file_name="approved_invoices.json",
        mime="application/json",
        disabled=not rows,
        key="download_json",
    )


def _detail(services: Services, candidate: InvoiceCandidate) -> None:
    review = services.review
    terminal = helpers.is_terminal(candidate)
    reviewer = st.text_input("Reviewer label", value="Reviewer", key="reviewer_label")
    reveal_key = f"reveal_{candidate.id}"
    reveal = bool(st.session_state.get(reveal_key))

    st.markdown(f"**Status:** `{candidate.status.value}`")
    st.dataframe(_comparison(candidate, reveal), hide_index=True)

    st.markdown("**Validation issues**")
    if candidate.issues:
        for issue in candidate.issues:
            where = f" ({FIELD_LABELS[issue.field_name]})" if issue.field_name else ""
            st.markdown(
                f"- `{issue.severity.value}` **{issue.code.value}**{where}: {issue.message}"
            )
    else:
        st.caption("No validation issues.")

    st.markdown("**Correct values**")
    _sync_edit_fields(candidate)
    for name in ALL_FIELDS:
        if name == "vendor_tax_id" and not reveal:
            if st.button("Edit tax ID", key="reveal_tax_id", disabled=terminal):
                st.session_state[reveal_key] = True
                st.rerun()
            continue
        st.text_input(FIELD_LABELS[name], key=f"field_{name}", disabled=terminal)
    if st.button("Save corrections", key="save_corrections", disabled=terminal):
        changes = {}
        current = candidate.effective_raw.model_dump()
        for name in ALL_FIELDS:
            if name == "vendor_tax_id" and not reveal:
                continue
            new_value = st.session_state[f"field_{name}"].strip()
            if new_value != (current[name] or "").strip():
                changes[name] = new_value or None
        _perform(
            lambda: review.correct(candidate.id, changes, reviewer_label=reviewer),
            "Corrections saved and validation re-run.",
        )

    st.markdown("**Confirm low-confidence fields**")
    options = helpers.confirmable_for(candidate)
    chosen = st.multiselect(
        "Fields to confirm", options, key="confirm_field_names", disabled=terminal
    )
    if st.button("Confirm selected fields", key="confirm_fields", disabled=terminal or not options):
        _perform(
            lambda: review.confirm_fields(candidate.id, set(chosen), reviewer_label=reviewer),
            "Fields confirmed.",
        )

    st.markdown("**Possible duplicate**")
    duplicate = helpers.has_unresolved_duplicate(candidate)
    distinct_reason = st.text_input("Why is this invoice distinct?", key="distinct_reason")
    if st.button(
        "Confirm distinct",
        key="confirm_distinct",
        disabled=terminal or not duplicate,
    ):
        _perform(
            lambda: review.confirm_distinct_duplicate(
                candidate.id, reviewer_label=reviewer, reason=distinct_reason
            ),
            "Marked as distinct from the compared candidates.",
        )

    st.markdown("**Decision**")
    reject_reason = st.text_input("Rejection reason", key="reject_reason")
    approve_col, reject_col = st.columns(2)
    if approve_col.button("Approve", key="approve", disabled=terminal):
        _perform(
            lambda: review.approve(candidate.id, reviewer_label=reviewer),
            "Candidate approved.",
        )
    if reject_col.button("Reject", key="reject", disabled=terminal):
        _perform(
            lambda: review.reject(candidate.id, reviewer_label=reviewer, reason=reject_reason),
            "Candidate rejected.",
        )

    st.markdown("**Append-only timeline**")
    _timeline(services, candidate)


def render() -> None:
    services = helpers.get_services()
    st.header("Review Queue")
    _show_flash()
    choice = st.selectbox(
        "Status filter",
        ["all", *(s.value for s in CandidateStatus)],
        key="status_filter",
    )
    status = None if choice == "all" else CandidateStatus(choice)
    candidates = services.database.list_candidates(status)
    if not candidates:
        st.info("No candidates to show. Upload a synthetic invoice first.")
    else:
        st.dataframe(
            pd.DataFrame([helpers.summarize_candidate(c) for c in candidates]),
            hide_index=True,
        )
        by_id = {c.id: c for c in candidates}
        selected = st.selectbox(
            "Open candidate",
            list(by_id),
            format_func=lambda cid: (
                f"{cid[:8]} - {by_id[cid].status.value} - "
                f"{by_id[cid].effective_raw.invoice_number or 'no invoice number'}"
            ),
            key="candidate_select",
        )
        _detail(services, by_id[selected])
    st.divider()
    _export_section(services)
