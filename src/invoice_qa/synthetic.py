"""Deterministic synthetic invoice PDFs, shared by test factories and the sample generator."""

from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

_FIXED_TITLE = "Synthetic Demo Invoice"
_FIXED_SUBJECT = "Synthetic data for portfolio demonstration"
_FIXED_AUTHOR = "Example Demo Generator"
_FIXED_CREATOR = "invoice-qa-assistant synthetic builder"
_FIXED_PRODUCER = "ReportLab (invariant mode)"

_VARIANT_SUBJECTS = {
    "canonical": _FIXED_SUBJECT,
    "alternate": _FIXED_SUBJECT + " (alternate)",
}


def build_invoice_pdf(lines: list[str], *, metadata_variant: str = "canonical") -> bytes:
    """Render ``lines`` top-down on one A4 page with fixed metadata and no timestamps.

    Byte reproducibility is only claimed for the exact pinned ReportLab version.
    """
    subject = _VARIANT_SUBJECTS[metadata_variant]
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=A4, pageCompression=0, invariant=1)
    canvas.setTitle(_FIXED_TITLE)
    canvas.setSubject(subject)
    canvas.setAuthor(_FIXED_AUTHOR)
    canvas.setCreator(_FIXED_CREATOR)
    canvas.setProducer(_FIXED_PRODUCER)
    canvas.setFont("Helvetica", 11)
    y = A4[1] - 60
    for line in lines:
        canvas.drawString(50, y, line)
        y -= 18
    canvas.showPage()
    canvas.save()
    return buffer.getvalue()
