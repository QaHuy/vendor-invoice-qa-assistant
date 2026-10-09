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
        successful = data["successful_candidates"]
        review_required = data["status_counts"]["review_required"]
        data["review_rate"] = review_required / successful if successful else None
        return data

    def evaluation_summary(self) -> dict[str, Any] | None:
        try:
            report = json.loads(self._report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        aggregate = report.get("aggregate") if isinstance(report, dict) else None
        return aggregate if isinstance(aggregate, dict) else None
