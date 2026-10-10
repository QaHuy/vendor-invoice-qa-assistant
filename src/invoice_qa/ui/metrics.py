"""Metrics page: database facts versus evaluation-only numbers."""

import streamlit as st

from invoice_qa.services.metrics import EVALUATE_COMMAND
from invoice_qa.ui import helpers


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def render() -> None:
    st.header("Metrics")
    data = helpers.get_services().metrics.snapshot()
    evaluation = helpers.get_services().metrics.evaluation_summary()

    st.subheader("Operational facts (from the local database)")
    counts = data["status_counts"]
    columns = st.columns(4)
    columns[0].metric("Processed sources", data["sources"])
    columns[1].metric("Extraction attempts", data["attempts"])
    columns[2].metric("Failed attempts", data["extraction_errors"])
    columns[3].metric("Content duplicates", data["content_duplicates"])
    columns = st.columns(4)
    columns[0].metric("Logical duplicate flags", data["logical_duplicate_flags"])
    columns[1].metric("Auto-ready", counts["auto_ready"])
    columns[2].metric("Review required", counts["review_required"])
    columns[3].metric("Approved / rejected", f"{counts['approved']} / {counts['rejected']}")
    columns = st.columns(3)
    columns[0].metric("Initial review rate", _percent(data["initial_review_rate"]))
    columns[1].metric(
        "Current review backlog share", _percent(data["current_review_backlog_share"])
    )
    columns[2].metric("Retry attempts", data["retry_attempts"])
    average = data["average_original_confidence_bps"]
    st.caption(
        "Initial review rate: candidates routed to review when created / candidates with "
        "recorded routing; it never changes after review. Current review backlog share: "
        "candidates waiting for review now / all candidates. "
        f"Average original confidence: {'n/a' if average is None else f'{average} bps'}."
    )
    if data["initial_routing_unknown"]:
        st.caption(
            f"{data['initial_routing_unknown']} candidate(s) predate routing records and are "
            "excluded from the initial review rate."
        )

    st.subheader("Evaluation-only metrics (synthetic, local)")
    if evaluation is None:
        st.info("No evaluation report found. Produce one with:")
        st.code(EVALUATE_COMMAND, language="bash")
        return
    columns = st.columns(4)
    columns[0].metric("False auto-ready", evaluation.get("false_auto_ready", "n/a"))
    columns[1].metric("False review", evaluation.get("false_review", "n/a"))
    columns[2].metric("Field accuracy", _percent(evaluation.get("field_accuracy")))
    columns[3].metric(
        "Evaluation initial review rate", _percent(evaluation.get("initial_review_rate"))
    )
    st.caption("Results come from the committed synthetic manifest only.")
