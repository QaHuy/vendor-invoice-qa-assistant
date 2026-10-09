"""Approved-only export of effective values. Vendor tax IDs are masked; no evidence leaves."""

import csv
import io
import json

from invoice_qa.domain.models import ALL_FIELDS, CandidateStatus
from invoice_qa.domain.validation import mask_tax_id
from invoice_qa.infrastructure.database import Database

EXPORT_FIELDS: tuple[str, ...] = ALL_FIELDS


def approved_rows(database: Database) -> list[dict[str, str | None]]:
    rows = []
    for candidate in database.list_candidates(CandidateStatus.APPROVED):
        values = candidate.effective_normalized.to_json_dict()
        values["vendor_tax_id"] = mask_tax_id(values["vendor_tax_id"]) or None
        rows.append({name: values[name] for name in EXPORT_FIELDS})
    return rows


def _csv_safe(value: str | None) -> str:
    """Neutralize spreadsheet formula injection by prefixing risky leading characters."""
    text = value or ""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def rows_to_csv(rows: list[dict[str, str | None]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EXPORT_FIELDS)
    for row in rows:
        writer.writerow([_csv_safe(row[name]) for name in EXPORT_FIELDS])
    return buffer.getvalue()


def rows_to_json(rows: list[dict[str, str | None]]) -> str:
    return json.dumps(rows, indent=2)
