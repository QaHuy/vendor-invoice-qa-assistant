import importlib.util
import json
import re
from datetime import date
from pathlib import Path

import pytest

from conftest import TODAY
from invoice_qa.domain.models import AttemptStatus, CandidateStatus, IssueCode
from invoice_qa.extraction.deterministic import DeterministicExtractor
from invoice_qa.infrastructure.file_security import sha256_bytes, validate_pdf_upload
from invoice_qa.services.intake import IntakeOutcome, IntakeService
from invoice_qa.synthetic import build_no_text_pdf, fixture_pdf

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "sample_data" / "manifest.json"

REQUIRED_SCENARIOS = {
    "clean_usd",
    "clean_vnd",
    "clean_no_po",
    "missing_invoice_number",
    "unsupported_currency",
    "malformed_amount",
    "arithmetic_mismatch",
    "future_invoice_date",
    "low_confidence_alias",
    "conflicting_total",
    "content_duplicate",
    "logical_duplicate",
    "excess_fraction_digits",
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator():
    return _load_module("generate_samples", ROOT / "sample_data" / "generate_samples.py")


@pytest.fixture(scope="module")
def evaluator():
    return _load_module("evaluate", ROOT / "scripts" / "evaluate.py")


@pytest.fixture(scope="module")
def manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_all_thirteen_required_scenarios_exist(manifest):
    scenarios = [f["scenario"] for f in manifest["fixtures"]]
    assert len(scenarios) == len(set(scenarios))
    assert set(scenarios) == REQUIRED_SCENARIOS
    assert len({f["id"] for f in manifest["fixtures"]}) == len(manifest["fixtures"])


def _label_value(line: str) -> tuple[str, str]:
    label, _, value = line.partition(":")
    return re.sub(r"[^a-z]", "", label.lower()), value.strip()


def test_fictional_data_convention(manifest):
    for fixture in manifest["fixtures"]:
        for line in fixture["lines"]:
            label, value = _label_value(line)
            if label == "vendorname":
                assert "Example" in value or "Demo" in value, fixture["id"]
            elif label == "vendortaxid":
                assert value.startswith("DEMO-"), fixture["id"]
            elif label == "invoicenumber":
                assert value.startswith("INV-DEMO-"), fixture["id"]
            elif label == "poreference":
                assert value.startswith("PO-DEMO-"), fixture["id"]


def test_no_person_address_phone_email_or_bank_details(manifest):
    forbidden = [
        r"@",
        r"https?://",
        r"\bwww\.",
        r"\+?\d{1,3}[ -]?\(?\d{3}\)?[ -]\d{3}[ -]\d{4}",
        r"\b(street|st\.|avenue|road|rd\.|suite|floor)\b",
        r"\b(iban|swift|routing|account (no|number)|bank)\b",
        r"\b(mr|mrs|ms|dr)\.? ",
    ]
    allowed_labels = {
        "vendorname",
        "vendortaxid",
        "invoicenumber",
        "invoicedate",
        "currency",
        "subtotal",
        "taxamount",
        "totalamount",
        "poreference",
    }
    for fixture in manifest["fixtures"]:
        for line in fixture["lines"]:
            assert _label_value(line)[0] in allowed_labels, line
            for pattern in forbidden:
                assert not re.search(pattern, line, re.IGNORECASE), (fixture["id"], pattern)


def test_generated_pdfs_pass_the_upload_validator(manifest):
    for fixture in manifest["fixtures"]:
        pdf = fixture_pdf(fixture)
        assert validate_pdf_upload(f"{fixture['id']}.pdf", pdf).page_count == 1


def test_two_generations_have_identical_hashes(generator, tmp_path):
    first = generator.generate(MANIFEST_PATH, tmp_path / "one")
    second = generator.generate(MANIFEST_PATH, tmp_path / "two")
    assert first == second
    for fixture_id, digest in first.items():
        assert sha256_bytes((tmp_path / "one" / f"{fixture_id}.pdf").read_bytes()) == digest
        assert (tmp_path / "one" / f"{fixture_id}.pdf").read_bytes() == (
            tmp_path / "two" / f"{fixture_id}.pdf"
        ).read_bytes()


def test_generator_reuses_the_shared_pdf_builder():
    source = (ROOT / "sample_data" / "generate_samples.py").read_text(encoding="utf-8")
    assert "fixture_pdf" in source
    assert "reportlab" not in source.lower()
    assert "canvas" not in source.lower()


def test_content_and_logical_duplicates_have_the_right_byte_relationship(manifest):
    pdfs = {f["id"]: fixture_pdf(f) for f in manifest["fixtures"]}
    for fixture in manifest["fixtures"]:
        duplicate = fixture["expected"]["duplicate"]
        if duplicate is None:
            continue
        same_bytes = pdfs[fixture["id"]] == pdfs[duplicate["of"]]
        assert same_bytes is (duplicate["kind"] == "content"), fixture["id"]


def test_manifest_uses_domain_enums(manifest):
    statuses = {s.value for s in CandidateStatus}
    codes = {c.value for c in IssueCode}
    for fixture in manifest["fixtures"]:
        expected = fixture["expected"]
        assert expected["status"] in statuses
        assert set(expected["issue_codes"]) <= codes
        assert expected["issue_codes"] == sorted(expected["issue_codes"])


def test_duplicate_relationships_point_to_earlier_fixtures(manifest):
    seen = []
    for fixture in manifest["fixtures"]:
        duplicate = fixture["expected"]["duplicate"]
        if duplicate is not None:
            assert duplicate["of"] in seen
            assert duplicate["kind"] in {"content", "logical"}
        seen.append(fixture["id"])


def test_reference_date_is_valid_iso(manifest):
    assert date.fromisoformat(manifest["validation_reference_date"])


# ------------------------------------------------------------------ evaluator


def test_evaluator_matches_the_manifest_with_zero_false_counts(evaluator, tmp_path):
    output = tmp_path / "report.json"
    code = evaluator.main(["--manifest", str(MANIFEST_PATH), "--output", str(output)])
    assert code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    aggregate = report["aggregate"]
    assert aggregate["false_auto_ready"] == 0
    assert aggregate["false_review"] == 0
    assert aggregate["scenarios_total"] == 13
    assert aggregate["scenarios_passed"] == 13
    assert aggregate["field_accuracy"] == 1.0
    assert 0 < aggregate["initial_review_rate"] < 1
    assert "review_rate" not in aggregate
    assert all(item["passed"] for item in report["scenarios"])
    assert "\\" not in output.read_text(encoding="utf-8")


def test_evaluator_reports_scenarios_in_manifest_order_with_expected_and_actual(
    evaluator, manifest, tmp_path
):
    report = evaluator.run_evaluation(MANIFEST_PATH)
    assert [s["id"] for s in report["scenarios"]] == [f["id"] for f in manifest["fixtures"]]
    first = report["scenarios"][0]
    assert first["expected"]["status"] == first["actual"]["status"] == "auto_ready"
    assert first["actual"]["issue_codes"] == []
    assert len(first["pdf_sha256"]) == 64


def test_evaluator_runs_against_a_fresh_database_each_time(evaluator):
    assert evaluator.run_evaluation(MANIFEST_PATH) == evaluator.run_evaluation(MANIFEST_PATH)


def _modified_manifest(manifest, tmp_path, mutate):
    data = json.loads(json.dumps(manifest))
    mutate(data)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_evaluator_fails_when_expected_routing_differs(evaluator, manifest, tmp_path):
    def mutate(data):
        next(f for f in data["fixtures"] if f["scenario"] == "arithmetic_mismatch")["expected"][
            "status"
        ] = "auto_ready"

    path = _modified_manifest(manifest, tmp_path, mutate)
    code = evaluator.main(["--manifest", str(path), "--output", str(tmp_path / "r.json")])
    assert code != 0
    aggregate = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["aggregate"]
    assert aggregate["false_review"] == 1
    assert aggregate["false_auto_ready"] == 0


def test_evaluator_counts_a_false_auto_ready(evaluator, manifest, tmp_path):
    def mutate(data):
        next(f for f in data["fixtures"] if f["scenario"] == "clean_usd")["expected"]["status"] = (
            "review_required"
        )

    path = _modified_manifest(manifest, tmp_path, mutate)
    assert evaluator.main(["--manifest", str(path), "--output", str(tmp_path / "r.json")]) != 0
    aggregate = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["aggregate"]
    assert aggregate["false_auto_ready"] == 1


def test_evaluator_fails_when_the_issue_code_set_differs(evaluator, manifest, tmp_path):
    def mutate(data):
        next(f for f in data["fixtures"] if f["scenario"] == "malformed_amount")["expected"][
            "issue_codes"
        ] = ["arithmetic_mismatch"]

    path = _modified_manifest(manifest, tmp_path, mutate)
    assert evaluator.main(["--manifest", str(path), "--output", str(tmp_path / "r.json")]) != 0


def test_reference_date_drives_the_future_date_scenario(evaluator, manifest, tmp_path):
    def mutate(data):
        data["validation_reference_date"] = "2026-10-12"

    path = _modified_manifest(manifest, tmp_path, mutate)
    assert evaluator.main(["--manifest", str(path), "--output", str(tmp_path / "r.json")]) != 0
    scenarios = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["scenarios"]
    future = next(s for s in scenarios if s["scenario"] == "future_invoice_date")
    assert future["actual"]["status"] == "auto_ready"


# ------------------------------------------------------------- retry walkthrough sample


def test_generator_writes_the_no_text_retry_sample_next_to_the_fixtures(generator, tmp_path):
    digests = generator.generate(MANIFEST_PATH, tmp_path)
    assert generator.RETRY_DEMO_ID in digests
    assert (tmp_path / "retry-demo-no-text.pdf").is_file()
    assert len(list(tmp_path.glob("*.pdf"))) == 14


def test_no_text_retry_sample_is_reproducible_and_distinct(generator, manifest, tmp_path):
    first = generator.generate(MANIFEST_PATH, tmp_path / "one")
    second = generator.generate(MANIFEST_PATH, tmp_path / "two")
    name = f"{generator.RETRY_DEMO_ID}.pdf"
    assert (tmp_path / "one" / name).read_bytes() == (tmp_path / "two" / name).read_bytes()
    assert first[generator.RETRY_DEMO_ID] == second[generator.RETRY_DEMO_ID]
    assert first[generator.RETRY_DEMO_ID] == sha256_bytes(build_no_text_pdf())
    fixture_digests = {first[f["id"]] for f in manifest["fixtures"]}
    assert first[generator.RETRY_DEMO_ID] not in fixture_digests


def test_no_text_retry_sample_is_a_valid_pdf_with_the_expected_extraction_error():
    pdf = build_no_text_pdf()
    assert validate_pdf_upload("retry-demo-no-text.pdf", pdf).page_count == 1
    result = DeterministicExtractor().extract(pdf)
    assert result.raw is None
    assert result.error_code == "no_extractable_text"


def test_no_text_retry_sample_drives_the_retry_flow(database):
    service = IntakeService(database, DeterministicExtractor(), today_provider=lambda: TODAY)
    pdf = build_no_text_pdf()
    first = service.process_upload("retry-demo-no-text.pdf", pdf)
    assert first.outcome is IntakeOutcome.EXTRACTION_ERROR and first.retryable
    assert first.error_code == "no_extractable_text"
    second = service.retry_extraction("retry-demo-no-text.pdf", pdf)
    assert second.error_code == "no_extractable_text" and second.retryable
    attempts = database.list_attempts_for_source(first.source_document_id)
    assert [(a.attempt_number, a.status) for a in attempts] == [
        (1, AttemptStatus.ERROR),
        (2, AttemptStatus.ERROR),
    ]


def test_no_text_retry_sample_is_not_an_evaluation_fixture(manifest):
    assert all("no-text" not in fixture["id"] for fixture in manifest["fixtures"])
    assert len(manifest["fixtures"]) == 13


def test_no_text_retry_sample_contains_no_personal_or_contact_text():
    assert b"@" not in build_no_text_pdf()
    assert b"http" not in build_no_text_pdf()
