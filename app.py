"""Vendor Invoice Digitization & QA Assistant (synthetic-data portfolio demo)."""

import streamlit as st

from invoice_qa.ui import about, metrics, review, upload

PAGES = {
    "Upload": upload.render,
    "Review Queue": review.render,
    "Metrics": metrics.render,
    "About": about.render,
}

st.set_page_config(page_title="Invoice QA Assistant", layout="wide")
st.sidebar.title("Invoice QA Assistant")
st.sidebar.caption("Synthetic data only. Not production software.")
page = st.sidebar.radio("Page", list(PAGES), key="page")
PAGES[page]()
