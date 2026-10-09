import csv
import io
import json

from factories import make_pdf
from invoice_qa.services.export import (
    EXPORT_FIELDS,
    approved_rows,
    rows_to_csv,
    rows_to_json,
)

REVIEWER = "Reviewer A"


def upload(intake, overrides=None, name="invoice.pdf"):
    return intake.process_upload(name, make_pdf(overrides)).candidate


def test_only_approved_candidates_export(intake, review, database):
    approved = upload(intake, name="a.pdf")
    upload(intake, {"Invoice Number": "INV-DEMO-0002"}, name="b.pdf")  # stays auto_ready
    rejected = upload(intake, {"Invoice Number": "INV-DEMO-0003"}, name="c.pdf")
    upload(intake, {"Invoice Number": "INV-DEMO-0004", "Subtotal": "1,0000"}, name="d.pdf")
    review.approve(approved.id, reviewer_label=REVIEWER)
    review.reject(rejected.id, reviewer_label=REVIEWER, reason="No")
    rows = approved_rows(database)
    assert [row["invoice_number"] for row in rows] == ["INV-DEMO-0001"]


def test_export_uses_effective_values_with_canonical_strings(intake, review, database):
    candidate = upload(intake, {"Subtotal": "1,0000"})
    review.correct(
        candidate.id, {"subtotal": "1,000.00", "tax_amount": "80.5"}, reviewer_label=REVIEWER
    )
    review.correct(candidate.id, {"total_amount": "1,080.50"}, reviewer_label=REVIEWER)
    review.approve(candidate.id, reviewer_label=REVIEWER)
    row = approved_rows(database)[0]
    assert row["subtotal"] == "1000.00"
    assert row["tax_amount"] == "80.50"
    assert row["total_amount"] == "1080.50"
    assert row["invoice_date"] == "2026-09-30"
    assert row["currency"] == "USD"


def test_csv_and_json_use_stable_field_names(intake, review, database):
    candidate = upload(intake)
    review.approve(candidate.id, reviewer_label=REVIEWER)
    rows = approved_rows(database)
    parsed_csv = list(csv.DictReader(io.StringIO(rows_to_csv(rows))))
    assert list(parsed_csv[0].keys()) == list(EXPORT_FIELDS)
    parsed_json = json.loads(rows_to_json(rows))
    assert list(parsed_json[0].keys()) == list(EXPORT_FIELDS)
    assert parsed_csv[0]["total_amount"] == parsed_json[0]["total_amount"] == "1080.00"


def test_export_masks_vendor_tax_id(intake, review, database):
    candidate = upload(intake)
    review.approve(candidate.id, reviewer_label=REVIEWER)
    rows = approved_rows(database)
    assert rows[0]["vendor_tax_id"] == "*********1001"
    assert "DEMO-TAX-1001" not in rows_to_csv(rows) + rows_to_json(rows)


def test_export_omits_internal_evidence(intake, review, database):
    candidate = upload(intake)
    review.approve(candidate.id, reviewer_label=REVIEWER)
    text = rows_to_csv(approved_rows(database)) + rows_to_json(approved_rows(database))
    for forbidden in ("confidence", "raw", "reason", "error", "issue", "Reviewer A", "original"):
        assert forbidden not in text.lower()


def test_empty_export_is_valid(database):
    assert approved_rows(database) == []
    assert rows_to_csv([]).strip() == ",".join(EXPORT_FIELDS)
    assert json.loads(rows_to_json([])) == []


def test_csv_neutralizes_spreadsheet_formulas(intake, review, database):
    candidate = upload(intake)
    review.correct(candidate.id, {"vendor_name": "=SUM(A1) Example Demo"}, reviewer_label=REVIEWER)
    review.approve(candidate.id, reviewer_label=REVIEWER)
    parsed = list(csv.DictReader(io.StringIO(rows_to_csv(approved_rows(database)))))
    assert parsed[0]["vendor_name"].startswith("'=")
