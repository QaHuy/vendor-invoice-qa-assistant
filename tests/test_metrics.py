import json

from factories import make_pdf, no_text_pdf
from fakes import flaky_service
from invoice_qa.services.metrics import EVALUATE_COMMAND, MetricsService

REVIEWER = "Reviewer A"


def test_zero_rows_are_handled(database, tmp_path):
    metrics = MetricsService(database, report_path=tmp_path / "missing.json").snapshot()
    assert metrics["sources"] == 0
    assert metrics["successful_candidates"] == 0
    assert metrics["initial_review_rate"] is None
    assert metrics["current_review_backlog_share"] is None
    assert "review_rate" not in metrics
    assert metrics["average_original_confidence_bps"] is None
    assert metrics["status_counts"] == {
        "auto_ready": 0,
        "review_required": 0,
        "approved": 0,
        "rejected": 0,
    }


def test_rate_denominators_are_successful_candidates(intake, review, database, tmp_path):
    intake.process_upload("a.pdf", make_pdf())
    intake.process_upload(
        "b.pdf", make_pdf({"Invoice Number": "INV-DEMO-0002", "Subtotal": "1,0000"})
    )
    intake.process_upload("scan.pdf", no_text_pdf())  # extraction error: not in denominator
    metrics = MetricsService(database, report_path=tmp_path / "missing.json").snapshot()
    assert metrics["successful_candidates"] == 2
    assert metrics["initial_review_rate"] == 0.5
    assert metrics["current_review_backlog_share"] == 0.5
    assert metrics["initial_status_counts"] == {"auto_ready": 1, "review_required": 1}
    assert metrics["initial_routing_unknown"] == 0


def test_initial_review_rate_survives_review_while_the_backlog_share_falls(
    intake, review, database, tmp_path
):
    clean = intake.process_upload("a.pdf", make_pdf()).candidate
    flagged = intake.process_upload(
        "b.pdf", make_pdf({"Invoice Number": "INV-DEMO-0002", "Subtotal": "1,0000"})
    ).candidate
    review.reject(flagged.id, reviewer_label=REVIEWER, reason="Not a valid invoice")
    review.approve(clean.id, reviewer_label=REVIEWER)
    metrics = MetricsService(database, report_path=tmp_path / "missing.json").snapshot()
    assert metrics["status_counts"]["review_required"] == 0
    assert metrics["current_review_backlog_share"] == 0.0
    assert metrics["initial_review_rate"] == 0.5


def test_correction_that_clears_issues_does_not_rewrite_the_initial_rate(
    intake, review, database, tmp_path
):
    flagged = intake.process_upload("b.pdf", make_pdf({"Subtotal": "1,0000"})).candidate
    review.correct(flagged.id, {"subtotal": "1,000.00"}, reviewer_label=REVIEWER)
    metrics = MetricsService(database, report_path=tmp_path / "missing.json").snapshot()
    assert metrics["status_counts"]["auto_ready"] == 1
    assert metrics["current_review_backlog_share"] == 0.0
    assert metrics["initial_review_rate"] == 1.0


def test_retry_attempts_and_failed_attempts_are_counted_separately(database, tmp_path):
    service, _ = flaky_service(database, failures=1)
    service.process_upload("a.pdf", make_pdf())
    service.retry_extraction("a.pdf", make_pdf())
    metrics = MetricsService(database, report_path=tmp_path / "missing.json").snapshot()
    assert (metrics["sources"], metrics["attempts"]) == (1, 2)
    assert (metrics["extraction_errors"], metrics["retry_attempts"]) == (1, 1)
    assert metrics["successful_candidates"] == 1


def test_extraction_error_and_duplicate_metrics_come_from_source_and_attempt_facts(
    intake, database, tmp_path
):
    intake.process_upload("scan.pdf", no_text_pdf())
    intake.process_upload("scan.pdf", no_text_pdf())
    intake.process_upload("a.pdf", make_pdf())
    intake.process_upload("a.pdf", make_pdf())
    intake.process_upload("a.pdf", make_pdf())
    metrics = MetricsService(database, report_path=tmp_path / "missing.json").snapshot()
    assert metrics["sources"] == 2
    assert metrics["attempts"] == 2
    assert metrics["extraction_errors"] == 1
    assert metrics["content_duplicates"] == 3
    assert metrics["average_original_confidence_bps"] == 10000


def test_logical_duplicate_flags_are_counted(intake, database, tmp_path):
    intake.process_upload("a.pdf", make_pdf())
    intake.process_upload("b.pdf", make_pdf(extra=["Note: copy"]))
    metrics = MetricsService(database, report_path=tmp_path / "missing.json").snapshot()
    assert metrics["logical_duplicate_flags"] == 1


def test_evaluation_metrics_are_absent_without_a_report_and_show_the_command(database, tmp_path):
    service = MetricsService(database, report_path=tmp_path / "missing.json")
    assert service.evaluation_summary() is None
    assert "scripts/evaluate.py" in EVALUATE_COMMAND
    assert "sample_data/manifest.json" in EVALUATE_COMMAND


def test_evaluation_summary_is_read_from_the_report(database, tmp_path):
    report = tmp_path / "report.json"
    aggregate = {"false_auto_ready": 0, "false_review": 0, "initial_review_rate": 0.5}
    report.write_text(json.dumps({"aggregate": aggregate, "scenarios": []}), encoding="utf-8")
    assert MetricsService(database, report_path=report).evaluation_summary() == aggregate


def test_unreadable_report_is_treated_as_absent(database, tmp_path):
    report = tmp_path / "report.json"
    report.write_text("not json", encoding="utf-8")
    assert MetricsService(database, report_path=report).evaluation_summary() is None
    report.write_text(json.dumps({"unexpected": 1}), encoding="utf-8")
    assert MetricsService(database, report_path=report).evaluation_summary() is None
