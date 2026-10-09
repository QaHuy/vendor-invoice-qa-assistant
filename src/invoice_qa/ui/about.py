"""About page."""

import streamlit as st

_ABOUT = """
**Synthetic-data portfolio demo.** Every invoice, organization and identifier here is
fictional. This is **not production software** and makes no claim of real-world accuracy,
compliance, customer usage or time savings.

### Business problem
Accounts-payable teams re-key semi-structured vendor invoices by hand, which costs time and
introduces identity, date, currency, tax, arithmetic, duplicate and purchase-order errors.
Fully automatic extraction is risky when layouts are unfamiliar, values conflict or confidence
is low.

### Workflow
Upload a PDF, pass file-security checks, extract text deterministically, keep raw evidence,
normalize exactly, validate, detect duplicates, route exceptions to a human, approve or
reject, export approved data, and keep an append-only audit history.

### Architecture
Streamlit pages call plain-Python services (intake, review, metrics, export) over a domain
layer (models, normalization, validation, transitions) and a local SQLite database. Uploaded
PDF bytes and full extracted text are never stored.

### Human controls
- A clean candidate becomes `auto_ready`; it still needs a deliberate human approval.
- Confirmation can resolve only low-confidence warnings; blocking issues need a valid correction.
- Approval re-runs validation, including duplicate detection.
- Approved and rejected candidates are terminal.
- Original extraction, normalized values and confidence are never overwritten.

### Security decisions
PDF only, at most 5 MiB, non-encrypted, structurally readable, at least one page. Vendor tax
IDs are masked everywhere except an explicit session-only edit action. The app is
network-free by default.

### Licenses
Project license: not yet selected. Dependencies include permissive-licensed libraries such as
pypdf and ReportLab; this is not legal advice.

### Known limitations
Narrow label-value format only; no OCR; one extraction attempt per document and no retry;
no authentication; no credit notes, multi-rate VAT, withholding tax or rounding adjustments;
rejected uploads are not counted in persisted metrics. Real-provider AI integration is out of
scope: only the protocol, a test-only fake and the validation boundary exist.

### Production hardening (not done)
Authentication and roles, managed database and retention policy, monitoring, reopen flow,
retry policy, OCR, ERP integration, and a legal review of data handling.
"""


def render() -> None:
    st.header("About")
    st.markdown(_ABOUT)
