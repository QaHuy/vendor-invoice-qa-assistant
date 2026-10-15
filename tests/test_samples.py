import json
from pathlib import Path

from invoice_qa.samples import DEFAULT_MANIFEST_PATH, load_samples
from invoice_qa.synthetic import fixture_pdf

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "sample_data" / "manifest.json"


def _fixtures() -> list[dict]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["fixtures"]


def test_samples_follow_manifest_order_without_the_date_sensitive_fixture():
    ids = [sample.id for sample in load_samples(MANIFEST)]
    expected = [f["id"] for f in _fixtures() if f["scenario"] != "future_invoice_date"]
    assert ids == expected
    assert len(ids) == 12


def test_sample_pdfs_match_the_shared_fixture_builder():
    fixtures = {f["id"]: f for f in _fixtures()}
    for sample in load_samples(MANIFEST):
        assert sample.build() == fixture_pdf(fixtures[sample.id])
        assert sample.filename == f"{sample.id}.pdf"


def test_sample_labels_are_readable_and_name_the_duplicate_source():
    labels = {sample.id: sample.label for sample in load_samples(MANIFEST)}
    assert labels["fx01-clean-usd"] == "Clean USD"
    assert labels["fx03-clean-no-po"] == "Clean no PO"
    assert labels["fx07-arithmetic-mismatch"] == "Arithmetic mismatch"
    assert labels["fx11-content-duplicate"] == "Content duplicate (run 'Clean USD' first)"
    assert labels["fx12-logical-duplicate"] == "Logical duplicate (run 'Clean USD' first)"
    assert len(set(labels.values())) == len(labels)


def test_missing_manifest_yields_no_samples(tmp_path):
    assert load_samples(tmp_path / "absent.json") == []


def test_default_manifest_path_is_relative_to_the_working_directory():
    assert DEFAULT_MANIFEST_PATH == Path("sample_data/manifest.json")
