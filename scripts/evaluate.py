"""Evaluate routing and extraction against the committed synthetic manifest.

Usage:
    python scripts/evaluate.py --manifest sample_data/manifest.json --output .evaluation/report.json

The exit code is non-zero when any scenario differs from the manifest.
"""

import argparse
import hashlib
import json
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

from invoice_qa.domain.models import CandidateStatus
from invoice_qa.extraction.deterministic import DeterministicExtractor
from invoice_qa.infrastructure.database import Database
from invoice_qa.infrastructure.file_security import sha256_bytes
from invoice_qa.services.intake import IntakeOutcome, IntakeService
from invoice_qa.synthetic import fixture_pdf


def run_evaluation(manifest_path: Path) -> dict:
    manifest_bytes = Path(manifest_path).read_bytes()
    manifest = json.loads(manifest_bytes)
    reference = date.fromisoformat(manifest["validation_reference_date"])
    fixed_now = datetime(reference.year, reference.month, reference.day, 12, tzinfo=UTC)

    scenarios = []
    with tempfile.TemporaryDirectory() as workdir:
        database = Database(Path(workdir) / "evaluation.sqlite3", now_provider=lambda: fixed_now)
        database.initialize()
        intake = IntakeService(database, DeterministicExtractor(), today_provider=lambda: reference)
        for fixture in manifest["fixtures"]:
            scenarios.append(_evaluate_fixture(intake, fixture))
    return {
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "validation_reference_date": reference.isoformat(),
        "label": "synthetic, local evaluation",
        "aggregate": _aggregate(scenarios),
        "scenarios": scenarios,
    }


def _evaluate_fixture(intake: IntakeService, fixture: dict) -> dict:
    pdf = fixture_pdf(fixture)
    result = intake.process_upload(f"{fixture['id']}.pdf", pdf)
    expected = fixture["expected"]
    candidate = result.candidate
    if result.outcome is IntakeOutcome.CANDIDATE:
        actual = {
            "status": candidate.status.value,
            "issue_codes": sorted({i.code.value for i in candidate.issues}),
            "overall_confidence_bps": candidate.original_overall_confidence_bps,
            "content_duplicate": result.content_duplicate,
        }
        normalized = candidate.original_normalized.to_json_dict()
    else:
        actual = {
            "status": result.outcome.value,
            "issue_codes": [],
            "overall_confidence_bps": None,
            "content_duplicate": result.content_duplicate,
        }
        normalized = {}
    is_content_duplicate = (expected["duplicate"] or {}).get("kind") == "content"
    truth = expected["normalized"]
    correct = sum(1 for name, value in truth.items() if normalized.get(name) == value)
    checks = {
        "status": actual["status"] == expected["status"],
        "issue_codes": actual["issue_codes"] == expected["issue_codes"],
        "overall_confidence_bps": (
            actual["overall_confidence_bps"] == expected["overall_confidence_bps"]
        ),
        "content_duplicate": actual["content_duplicate"] is is_content_duplicate,
        "normalized": correct == len(truth),
    }
    return {
        "id": fixture["id"],
        "scenario": fixture["scenario"],
        "pdf_sha256": sha256_bytes(pdf),
        "passed": all(checks.values()),
        "checks": checks,
        "expected": {
            "status": expected["status"],
            "issue_codes": expected["issue_codes"],
            "overall_confidence_bps": expected["overall_confidence_bps"],
            "duplicate": expected["duplicate"],
        },
        "actual": actual,
        "fields_correct": correct,
        "fields_total": len(truth),
        "counts_as_new_routing": not is_content_duplicate,
    }


def _aggregate(scenarios: list[dict]) -> dict:
    routed = [s for s in scenarios if s["counts_as_new_routing"]]
    review_required = CandidateStatus.REVIEW_REQUIRED.value
    auto_ready = CandidateStatus.AUTO_READY.value
    false_auto = sum(
        1
        for s in routed
        if s["expected"]["status"] == review_required and s["actual"]["status"] == auto_ready
    )
    false_review = sum(
        1
        for s in routed
        if s["expected"]["status"] == auto_ready and s["actual"]["status"] == review_required
    )
    candidates = [s for s in routed if s["actual"]["status"] in (auto_ready, review_required)]
    fields_correct = sum(s["fields_correct"] for s in scenarios)
    fields_total = sum(s["fields_total"] for s in scenarios)
    review_rate = None
    if candidates:
        flagged = sum(1 for s in candidates if s["actual"]["status"] == review_required)
        review_rate = flagged / len(candidates)
    return {
        "scenarios_total": len(scenarios),
        "scenarios_passed": sum(1 for s in scenarios if s["passed"]),
        "fields_correct": fields_correct,
        "fields_total": fields_total,
        "field_accuracy": fields_correct / fields_total if fields_total else None,
        "review_rate": review_rate,
        "false_auto_ready": false_auto,
        "false_review": false_review,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_evaluation(args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    aggregate = report["aggregate"]
    for scenario in report["scenarios"]:
        print(f"{'PASS' if scenario['passed'] else 'FAIL'}  {scenario['id']}")
    rate = aggregate["review_rate"]
    print(
        f"scenarios {aggregate['scenarios_passed']}/{aggregate['scenarios_total']}  "
        f"field_accuracy {aggregate['fields_correct']}/{aggregate['fields_total']}  "
        f"review_rate {'n/a' if rate is None else format(rate, '.4f')}  "
        f"false_auto_ready {aggregate['false_auto_ready']}  "
        f"false_review {aggregate['false_review']}"
    )
    return 0 if aggregate["scenarios_passed"] == aggregate["scenarios_total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
