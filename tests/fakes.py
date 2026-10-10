"""Test-only fakes. The application never imports this module."""

from conftest import TODAY
from invoice_qa.extraction.base import ExtractionResult
from invoice_qa.extraction.deterministic import DeterministicExtractor
from invoice_qa.services.intake import IntakeService


class FakeAIProvider:
    """Simulates a structured-output provider without any network or API key."""

    def __init__(self, mode: str = "valid") -> None:
        self.mode = mode
        self.calls: list[str] = []

    def extract_structured(self, text: str) -> dict[str, object]:
        self.calls.append(text)
        if self.mode == "timeout":
            raise TimeoutError(f"provider timed out while reading: {text}")
        if self.mode == "error":
            raise RuntimeError(f"provider failed with payload: {text}")
        if self.mode == "invalid_schema":
            return {"fields": {"vendor_name": {"value": 12, "confidence_bps": "high"}}}
        if self.mode == "out_of_range":
            return {"fields": {"vendor_name": {"value": "Example Demo", "confidence_bps": 20000}}}
        return {
            "fields": {
                "vendor_name": {
                    "value": "Example Demo Office Supplies Ltd.",
                    "confidence_bps": 9500,
                },
                "invoice_number": {"value": "INV-DEMO-0001", "confidence_bps": 9000},
                "currency": {"value": None, "confidence_bps": 0},
            }
        }


class FlakyExtractor:
    """Fails the first ``failures`` calls with a safe code, then behaves like the real one."""

    method = "deterministic"

    def __init__(self, failures: int, error_code: str = "extraction_failed") -> None:
        self.failures = failures
        self.error_code = error_code
        self.calls = 0
        self._real = DeterministicExtractor()

    def extract(self, pdf_bytes: bytes) -> ExtractionResult:
        self.calls += 1
        if self.calls <= self.failures:
            return ExtractionResult(method=self.method, error_code=self.error_code)
        return self._real.extract(pdf_bytes)


def flaky_service(database, failures, **kwargs):
    extractor = FlakyExtractor(failures, **kwargs)
    return IntakeService(database, extractor, today_provider=lambda: TODAY), extractor
