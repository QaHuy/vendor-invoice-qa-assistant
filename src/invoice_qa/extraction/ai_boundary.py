"""AI extraction boundary: protocol plus Pydantic validation of provider output.

No real provider is connected in this MVP; the application always uses the deterministic
extractor. Errors carry stable codes only, never source text or provider payloads.
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from invoice_qa.domain.models import ALL_FIELDS, MAX_BPS, RawInvoiceFields
from invoice_qa.extraction.base import ExtractionResult, read_pdf_text


class AIProvider(Protocol):
    def extract_structured(self, text: str) -> dict[str, object]: ...


class AIField(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str | None
    confidence_bps: int = Field(ge=0, le=MAX_BPS)


class AIProviderOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: dict[str, AIField]


class AIExtractor:
    method = "ai"

    def __init__(self, provider: AIProvider) -> None:
        self._provider = provider

    def extract(self, pdf_bytes: bytes) -> ExtractionResult:
        try:
            text = read_pdf_text(pdf_bytes)
        except Exception:
            return self._error("extraction_failed")
        if not text.strip():
            return self._error("no_extractable_text")
        try:
            payload = self._provider.extract_structured(text)
        except TimeoutError:
            return self._error("ai_timeout")
        except Exception:
            return self._error("ai_provider_error")
        try:
            output = AIProviderOutput.model_validate(payload)
        except ValidationError:
            return self._error("ai_invalid_output")
        if not set(output.fields) <= set(ALL_FIELDS):
            return self._error("ai_invalid_output")
        values: dict[str, str | None] = {}
        confidence: dict[str, int] = {}
        for name in ALL_FIELDS:
            field = output.fields.get(name)
            values[name] = field.value if field else None
            confidence[name] = field.confidence_bps if field else 0
        return ExtractionResult(
            method=self.method,
            raw=RawInvoiceFields(**values),
            field_confidence_bps=confidence,
        )

    def _error(self, code: str) -> ExtractionResult:
        return ExtractionResult(method=self.method, error_code=code)
