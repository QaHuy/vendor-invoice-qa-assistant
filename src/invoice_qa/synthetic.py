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


def _new_canvas(buffer: BytesIO, subject: str) -> Canvas:
    canvas = Canvas(buffer, pagesize=A4, pageCompression=0, invariant=1)
    canvas.setTitle(_FIXED_TITLE)
    canvas.setSubject(subject)
    canvas.setAuthor(_FIXED_AUTHOR)
    canvas.setCreator(_FIXED_CREATOR)
    canvas.setProducer(_FIXED_PRODUCER)
    return canvas


def build_invoice_pdf(lines: list[str], *, metadata_variant: str = "canonical") -> bytes:
    """Render ``lines`` top-down on one A4 page with fixed metadata and no timestamps.

    Byte reproducibility is only claimed for the exact pinned ReportLab version.
    """
    buffer = BytesIO()
    canvas = _new_canvas(buffer, _VARIANT_SUBJECTS[metadata_variant])
    canvas.setFont("Helvetica", 11)
    y = A4[1] - 60
    for line in lines:
        canvas.drawString(50, y, line)
        y -= 18
    canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def build_no_text_pdf() -> bytes:
    """A valid one-page PDF that only draws a shape, so it has no text layer.

    The deterministic extractor reports ``no_extractable_text`` for it; the sample generator
    ships it so the manual retry walkthrough needs no code. Same reproducibility caveat as above.
    """
    buffer = BytesIO()
    canvas = _new_canvas(buffer, _FIXED_SUBJECT + " (no text layer)")
    canvas.rect(50, A4[1] - 200, 300, 120, stroke=1, fill=0)
    canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def fixture_pdf(fixture: dict) -> bytes:
    """Build the PDF for one manifest fixture (lines plus optional metadata variant)."""
    return build_invoice_pdf(
        list(fixture["lines"]),
        metadata_variant=fixture.get("metadata_variant", "canonical"),
    )
