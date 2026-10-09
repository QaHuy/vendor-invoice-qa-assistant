import pytest

from factories import make_pdf, malformed_pdf, no_text_pdf
from fakes import FakeAIProvider
from invoice_qa.domain.models import REQUIRED_FIELDS
from invoice_qa.domain.validation import effective_confidence
from invoice_qa.extraction.ai_boundary import AIExtractor
from invoice_qa.extraction.base import ExtractionResult
from invoice_qa.extraction.deterministic import DeterministicExtractor


def extract(overrides=None, extra=None) -> ExtractionResult:
    return DeterministicExtractor().extract(make_pdf(overrides, extra))


def test_canonical_fields_extract_as_raw_strings_with_full_confidence():
    result = extract()
    assert result.error_code is None
    assert result.method == "deterministic"
    assert result.raw.vendor_name == "Example Demo Office Supplies Ltd."
    assert result.raw.subtotal == "1,000.00"
    assert result.raw.invoice_date == "2026-09-30"
    assert result.raw.po_reference == "PO-DEMO-001"
    assert all(result.field_confidence_bps[name] == 10000 for name in REQUIRED_FIELDS)
    assert result.conflicts == []


def test_documented_alias_gets_9000():
    result = extract({"Invoice Number": None}, ["Invoice No: INV-DEMO-0002"])
    assert result.raw.invoice_number == "INV-DEMO-0002"
    assert result.field_confidence_bps["invoice_number"] == 9000


def test_loose_alias_gets_7500_only_for_its_declared_field():
    result = extract(
        {"Vendor Name": None, "Invoice Number": None},
        [
            "Vendor_Name: Example Demo Loose Ltd.",
            "Invoice_Number: INV-DEMO-0003",
        ],
    )
    assert result.raw.vendor_name == "Example Demo Loose Ltd."
    assert result.field_confidence_bps["vendor_name"] == 7500
    assert result.raw.invoice_number is None
    assert result.field_confidence_bps["invoice_number"] == 0


def test_omitted_labels_remain_none_with_zero_confidence():
    result = extract({"Invoice Number": None, "PO Reference": None})
    assert result.raw.invoice_number is None
    assert result.raw.po_reference is None
    assert result.field_confidence_bps["invoice_number"] == 0
    assert result.conflicts == []


def test_malformed_amount_remains_raw():
    result = extract({"Subtotal": "1,0000"})
    assert result.raw.subtotal == "1,0000"


def test_repeated_same_value_is_not_a_conflict():
    result = extract(extra=["Total Amount: 1,080.00"])
    assert result.raw.total_amount == "1,080.00"
    assert result.field_confidence_bps["total_amount"] == 10000
    assert result.conflicts == []


def test_repeated_different_values_create_a_conflict():
    result = extract(extra=["Total Amount: 1,090.00"])
    assert result.raw.total_amount is None
    assert result.field_confidence_bps["total_amount"] == 0
    assert len(result.conflicts) == 1
    assert result.conflicts[0].field_name == "total_amount"
    assert result.conflicts[0].values == ["1,080.00", "1,090.00"]


def test_overall_confidence_for_conflict_is_floor_mean():
    result = extract(extra=["Total Amount: 1,090.00"])
    _, overall = effective_confidence(result.field_confidence_bps, set(), set())
    assert overall == 8571


def test_pdf_without_text_returns_safe_error_code():
    result = DeterministicExtractor().extract(no_text_pdf())
    assert result.error_code == "no_extractable_text"
    assert result.raw is None


def test_unreadable_bytes_return_generic_extraction_failed():
    result = DeterministicExtractor().extract(malformed_pdf())
    assert result.error_code == "extraction_failed"


def test_result_never_carries_full_document_text():
    result = extract(extra=["Free text that is not a field: secret-looking-note"])
    assert "secret-looking-note" not in result.model_dump_json()


# --------------------------------------------------------------- AI boundary


def test_ai_extractor_accepts_valid_structured_output():
    provider = FakeAIProvider("valid")
    result = AIExtractor(provider).extract(make_pdf())
    assert result.error_code is None
    assert result.method == "ai"
    assert result.raw.vendor_name == "Example Demo Office Supplies Ltd."
    assert result.field_confidence_bps["vendor_name"] == 9500
    assert result.field_confidence_bps["subtotal"] == 0
    assert "Vendor Name: Example Demo" in provider.calls[0]


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("timeout", "ai_timeout"),
        ("error", "ai_provider_error"),
        ("invalid_schema", "ai_invalid_output"),
        ("out_of_range", "ai_invalid_output"),
    ],
)
def test_ai_failures_return_safe_codes_without_source_text(mode, code):
    result = AIExtractor(FakeAIProvider(mode)).extract(make_pdf())
    assert result.error_code == code
    assert result.raw is None
    dumped = result.model_dump_json() + repr(result)
    assert "Example Demo" not in dumped
    assert "INV-DEMO" not in dumped


def test_ai_extractor_reports_no_text_without_calling_provider():
    provider = FakeAIProvider("valid")
    result = AIExtractor(provider).extract(no_text_pdf())
    assert result.error_code == "no_extractable_text"
    assert provider.calls == []


def test_ai_boundary_needs_no_network_package_or_api_key(monkeypatch):
    import sys

    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    import invoice_qa.extraction.ai_boundary  # noqa: F401

    assert not {"openai", "anthropic", "requests", "httpx"} & set(sys.modules)
