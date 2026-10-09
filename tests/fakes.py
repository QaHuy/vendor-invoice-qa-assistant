"""Test-only fake AI provider. The application never imports this module."""


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
