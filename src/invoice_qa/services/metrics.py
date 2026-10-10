"""Operational metrics from database facts; evaluation metrics only from the evaluator's report."""

import json
from pathlib import Path
from typing import Any

from invoice_qa.infrastructure.database import Database

DEFAULT_REPORT_PATH = Path(".evaluation/report.json")
EVALUATE_COMMAND = (
    "python scripts/evaluate.py --manifest sample_data/manifest.json "
    "--output .evaluation/report.json"
)


class MetricsService:
    def __init__(self, database: Database, *, report_path: Path = DEFAULT_REPORT_PATH) -> None:
        self._db = database
        self._report_path = report_path

    def snapshot(self) -> dict[str, Any]:
        data = self._db.metrics_snapshot()
        # Initial rate: routing at creation (never changes). Backlog share: status right now.
        initial = data["initial_status_counts"]
        known = sum(initial.values())
        data["initial_review_rate"] = initial["review_required"] / known if known else None
        successful = data["successful_candidates"]
        waiting = data["status_counts"]["review_required"]
        data["current_review_backlog_share"] = waiting / successful if successful else None
        return data

    def evaluation_summary(self) -> dict[str, Any] | None:
        try:
            report = json.loads(self._report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        aggregate = report.get("aggregate") if isinstance(report, dict) else None
        return aggregate if isinstance(aggregate, dict) else None
