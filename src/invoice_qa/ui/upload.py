"""Upload page."""

import pandas as pd
import streamlit as st

from invoice_qa.services.intake import IntakeOutcome, IntakeResult
from invoice_qa.ui import helpers

_FORMAT_EXAMPLE = """Vendor Name: Example Demo Office Supplies Ltd.
Vendor Tax ID: DEMO-TAX-1001
Invoice Number: INV-DEMO-0001
Invoice Date: 2026-09-30
Currency: USD
Subtotal: 1,000.00
Tax Amount: 80.00
Total Amount: 1,080.00
PO Reference: PO-DEMO-001"""


def _show_result(result: IntakeResult) -> None:
    if result.outcome is IntakeOutcome.REJECTED:
        st.error(helpers.upload_rejection_message(result.rejection_code or ""))
        return
    if result.outcome is IntakeOutcome.IN_PROGRESS:
        st.info(helpers.extraction_in_progress_message())
        return
    if result.content_duplicate:
        st.info("This exact file was uploaded before. Showing the existing result.")
    if result.outcome is IntakeOutcome.EXTRACTION_ERROR:
        st.error(helpers.extraction_error_message(result.error_code or ""))
        return
    candidate = result.candidate
    st.success(f"Extracted with the deterministic extractor. Status: {candidate.status.value}")
    if candidate.issues:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Severity": i.severity.value,
                        "Issue": i.code.value,
                        "Field": i.field_name or "",
                        "Message": i.message,
                    }
                    for i in candidate.issues
                ]
            ),
            hide_index=True,
        )
    else:
        st.caption("No validation issues.")
    st.caption("Open the Review Queue to inspect, correct, approve or reject this candidate.")


def render() -> None:
    st.header("Upload")
    st.markdown(
        "Upload a **synthetic** vendor-invoice PDF. The demo extracts only the documented "
        "label-value format below; it never guesses and has no OCR."
    )
    st.code(_FORMAT_EXAMPLE, language="text")
    uploaded = st.file_uploader(
        "Synthetic invoice PDF", type=["pdf"], max_upload_size=5, key="invoice_upload"
    )
    if uploaded is None:
        st.caption("Nothing uploaded yet.")
        return
    processed: dict = st.session_state.setdefault("processed_uploads", {})
    services = helpers.get_services()
    if uploaded.file_id not in processed:
        processed[uploaded.file_id] = services.intake.process_upload(
            uploaded.name, uploaded.getvalue()
        )
    result = processed[uploaded.file_id]
    _show_result(result)
    # Retry is always an explicit click; nothing re-extracts on its own.
    if result.retryable and st.button("Retry extraction", key="retry_extraction"):
        processed[uploaded.file_id] = services.intake.retry_extraction(
            uploaded.name, uploaded.getvalue()
        )
        st.rerun()
