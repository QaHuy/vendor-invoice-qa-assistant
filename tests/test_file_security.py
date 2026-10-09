import pytest

from factories import (
    encrypted_pdf,
    exact_size_pdf,
    make_pdf,
    malformed_pdf,
    no_text_pdf,
    not_a_pdf,
    zero_page_pdf,
)
from invoice_qa.infrastructure import file_security
from invoice_qa.infrastructure.file_security import (
    MAX_UPLOAD_BYTES,
    UploadRejected,
    safe_display_filename,
    sha256_bytes,
    validate_pdf_upload,
)
from invoice_qa.synthetic import build_invoice_pdf


def rejection_code(filename, content, **kwargs) -> str:
    with pytest.raises(UploadRejected) as info:
        validate_pdf_upload(filename, content, **kwargs)
    return info.value.code


def test_valid_pdf_passes():
    inspection = validate_pdf_upload("invoice.pdf", make_pdf())
    assert inspection.page_count == 1
    assert inspection.size_bytes == len(make_pdf())


def test_no_text_pdf_is_not_a_security_rejection():
    assert validate_pdf_upload("scan.pdf", no_text_pdf()).page_count == 1


def test_renamed_non_pdf_fails_on_magic_bytes():
    assert rejection_code("invoice.pdf", not_a_pdf()) == "invalid_pdf_header"


def test_pdf_bytes_with_other_extension_fail():
    assert rejection_code("invoice.txt", make_pdf()) == "invalid_extension"


def test_extension_check_is_case_insensitive():
    assert validate_pdf_upload("INVOICE.PDF", make_pdf()).page_count == 1


def test_malformed_pdf_with_magic_bytes_fails_structural_parse():
    assert rejection_code("invoice.pdf", malformed_pdf()) == "unreadable_pdf"


def test_encrypted_pdf_fails():
    assert rejection_code("invoice.pdf", encrypted_pdf()) == "encrypted_pdf"


def test_zero_page_pdf_fails():
    assert rejection_code("invoice.pdf", zero_page_pdf()) == "no_pages"


def test_empty_upload_fails():
    assert rejection_code("invoice.pdf", b"") == "empty_file"


def test_exactly_5_mib_passes_when_structurally_valid():
    content = exact_size_pdf(MAX_UPLOAD_BYTES)
    assert len(content) == 5 * 1024 * 1024
    assert validate_pdf_upload("invoice.pdf", content).size_bytes == MAX_UPLOAD_BYTES


def test_one_byte_above_the_limit_fails():
    content = exact_size_pdf(MAX_UPLOAD_BYTES + 1)
    assert rejection_code("invoice.pdf", content) == "file_too_large"


def test_oversized_upload_fails_before_the_parser_is_called(monkeypatch):
    calls = []

    def spy(*args, **kwargs):
        calls.append(args)
        raise AssertionError("parser must not run for oversized content")

    monkeypatch.setattr(file_security, "PdfReader", spy)
    assert rejection_code("invoice.pdf", b"%PDF-" + b"0" * 100, max_bytes=50) == "file_too_large"
    assert calls == []


def test_checks_run_in_the_documented_order():
    big = b"x" * 100
    assert rejection_code("a.txt", b"", max_bytes=10) == "empty_file"
    assert rejection_code("a.txt", big, max_bytes=10) == "invalid_extension"
    assert rejection_code("a.pdf", big, max_bytes=10) == "file_too_large"
    assert rejection_code("a.pdf", big) == "invalid_pdf_header"
    assert rejection_code("a.pdf", malformed_pdf()) == "unreadable_pdf"
    assert rejection_code("a.pdf", encrypted_pdf()) == "encrypted_pdf"
    assert rejection_code("a.pdf", zero_page_pdf()) == "no_pages"


def test_rejection_message_is_generic():
    with pytest.raises(UploadRejected) as info:
        validate_pdf_upload("invoice.pdf", malformed_pdf())
    assert str(info.value) == "The file could not be accepted."
    assert "malformed" not in str(info.value).lower()


@pytest.mark.parametrize(
    "unsafe",
    ["../../etc/passwd", "C:\\Users\\x\\inv.pdf", "a\x00b<script>.pdf", "..", "", ".hidden.pdf"],
)
def test_unsafe_filenames_are_sanitized(unsafe):
    cleaned = safe_display_filename(unsafe)
    assert cleaned
    assert not set(cleaned) & set('/\\<>:"|?*\x00')
    assert not cleaned.startswith(".")
    assert len(cleaned) <= 100


def test_long_filenames_are_truncated():
    assert len(safe_display_filename("a" * 500 + ".pdf")) <= 100


def test_sha256_is_stable():
    digest = sha256_bytes(b"abc")
    assert digest == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert sha256_bytes(b"abc") == digest


def test_synthetic_pdf_builder_is_byte_reproducible():
    lines = ["Vendor Name: Example Demo Co"]
    assert build_invoice_pdf(lines) == build_invoice_pdf(lines)
    assert build_invoice_pdf(lines, metadata_variant="alternate") != build_invoice_pdf(lines)
