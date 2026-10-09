"""Test PDF factories. All content is synthetic and fictional."""

from io import BytesIO

from pypdf import PdfWriter

from invoice_qa.synthetic import build_invoice_pdf

CLEAN_FIELDS: dict[str, str] = {
    "Vendor Name": "Example Demo Office Supplies Ltd.",
    "Vendor Tax ID": "DEMO-TAX-1001",
    "Invoice Number": "INV-DEMO-0001",
    "Invoice Date": "2026-09-30",
    "Currency": "USD",
    "Subtotal": "1,000.00",
    "Tax Amount": "80.00",
    "Total Amount": "1,080.00",
    "PO Reference": "PO-DEMO-001",
}


def invoice_lines(overrides: dict[str, str | None] | None = None, extra: list[str] | None = None):
    """Label-value lines; an override of ``None`` omits the label."""
    fields = {**CLEAN_FIELDS, **(overrides or {})}
    lines = [f"{label}: {value}" for label, value in fields.items() if value is not None]
    return lines + (extra or [])


def make_pdf(overrides: dict[str, str | None] | None = None, extra: list[str] | None = None):
    return build_invoice_pdf(invoice_lines(overrides, extra))


def malformed_pdf() -> bytes:
    return b"%PDF-1.4\nthis is not a structurally valid document\n"


def not_a_pdf() -> bytes:
    return b"plain text, not a document"


def zero_page_pdf() -> bytes:
    buffer = BytesIO()
    PdfWriter().write(buffer)
    return buffer.getvalue()


def encrypted_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("demo-password", algorithm="RC4-128")
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def no_text_pdf() -> bytes:
    """A structurally valid one-page PDF without any text."""
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _padded(padding: int) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Subject": "x" * padding})
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def exact_size_pdf(size: int) -> bytes:
    """A structurally valid PDF of exactly ``size`` bytes, padded inside the info dictionary."""
    padding = max(size - len(_padded(0)), 0)
    for _ in range(6):
        data = _padded(padding)
        if len(data) == size:
            return data
        padding += size - len(data)
        if padding < 0:
            break
    raise AssertionError("could not build a PDF of the requested size")
