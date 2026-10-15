"""Synthetic sample invoices from the evaluation manifest, built in memory for the demo UI."""

import json
from dataclasses import dataclass
from pathlib import Path

from invoice_qa.synthetic import fixture_pdf

# Relative to the working directory, like the default database path in config.
DEFAULT_MANIFEST_PATH = Path("sample_data/manifest.json")

# Its expected result depends on the manifest's fixed validation reference date, while the
# app validates against today, so the hosted demo would not show the documented outcome.
_DATE_SENSITIVE_SCENARIOS = {"future_invoice_date"}
_ACRONYMS = {"usd": "USD", "vnd": "VND", "po": "PO"}


@dataclass(frozen=True)
class SampleInvoice:
    id: str
    label: str
    fixture: dict

    @property
    def filename(self) -> str:
        return f"{self.id}.pdf"

    def build(self) -> bytes:
        return fixture_pdf(self.fixture)


def _scenario_label(scenario: str) -> str:
    words = [_ACRONYMS.get(word, word) for word in scenario.split("_")]
    text = " ".join(words)
    return text[0].upper() + text[1:]


def load_samples(manifest_path: Path = DEFAULT_MANIFEST_PATH) -> list[SampleInvoice]:
    """Return the demo-safe manifest fixtures in manifest order; empty if the file is absent."""
    path = Path(manifest_path)
    if not path.is_file():
        return []
    fixtures = json.loads(path.read_text(encoding="utf-8"))["fixtures"]
    base_labels = {f["id"]: _scenario_label(f["scenario"]) for f in fixtures}
    samples = []
    for fixture in fixtures:
        if fixture["scenario"] in _DATE_SENSITIVE_SCENARIOS:
            continue
        label = base_labels[fixture["id"]]
        duplicate = fixture["expected"].get("duplicate")
        if duplicate:
            label = f"{label} (run '{base_labels[duplicate['of']]}' first)"
        samples.append(SampleInvoice(fixture["id"], label, fixture))
    return samples
