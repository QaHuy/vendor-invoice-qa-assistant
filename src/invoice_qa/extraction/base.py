"""Extraction contracts shared by the deterministic and AI-boundary extractors."""

from io import BytesIO
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader

from invoice_qa.domain.models import FieldConflict, RawInvoiceFields


class ExtractionResult(BaseModel):
    """Raw fields and per-field confidence only. Never carries full document text."""

    model_config = ConfigDict(frozen=True)

    method: str
    raw: RawInvoiceFields | None = None
    field_confidence_bps: dict[str, int] = Field(default_factory=dict)
    conflicts: list[FieldConflict] = Field(default_factory=list)
    error_code: str | None = None


class Extractor(Protocol):
    def extract(self, pdf_bytes: bytes) -> ExtractionResult: ...


def read_pdf_text(pdf_bytes: bytes) -> str:
    """Return the text layer. Callers must not persist or log it."""
    reader = PdfReader(BytesIO(pdf_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)
