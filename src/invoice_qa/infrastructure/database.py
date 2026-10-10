"""SQLite persistence: sources, immutable extraction attempts, candidates and review events.

A short-lived connection is opened per repository operation (Streamlit sessions run on
different threads). PDF bytes and full extracted text are never stored.
"""

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from invoice_qa.config import get_db_path
from invoice_qa.domain.models import (
    AttemptRecord,
    AttemptStatus,
    CandidateStatus,
    FieldConflict,
    InvoiceCandidate,
    IssueCode,
    NormalizedInvoiceFields,
    RawInvoiceFields,
    ReviewEvent,
    ReviewEventType,
    SourceDocumentRecord,
    ValidationIssue,
)

_EVENT_TYPES = ", ".join(f"'{e.value}'" for e in ReviewEventType)
_STATUSES = ", ".join(f"'{s.value}'" for s in CandidateStatus)
_ATTEMPT_STATUSES = ", ".join(f"'{s.value}'" for s in AttemptStatus)
_OPEN_STATUSES = (
    CandidateStatus.AUTO_READY.value,
    CandidateStatus.REVIEW_REQUIRED.value,
    CandidateStatus.APPROVED.value,
)

SCHEMA_VERSION = 2


class RetryNotAllowed(Exception):
    """A retry was requested for a source that cannot be retried (unknown or already succeeded)."""


def _attempts_table(name: str, *, if_not_exists: bool) -> str:
    guard = "IF NOT EXISTS " if if_not_exists else ""
    return f"""
CREATE TABLE {guard}{name} (
    id TEXT PRIMARY KEY,
    source_document_id TEXT NOT NULL REFERENCES source_documents(id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    method TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ({_ATTEMPT_STATUSES})),
    error_code TEXT,
    raw_json TEXT,
    field_confidence_json TEXT,
    conflicts_json TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (source_document_id, attempt_number)
);
"""


_INITIAL_STATUS_COLUMN = (
    f"initial_status TEXT CHECK (initial_status IS NULL OR initial_status IN ({_STATUSES}))"
)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS source_documents (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    display_filename TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

{_attempts_table("extraction_attempts", if_not_exists=True)}
CREATE TABLE IF NOT EXISTS candidates (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES extraction_attempts(id),
    original_raw_json TEXT NOT NULL,
    original_normalized_json TEXT NOT NULL,
    original_field_confidence_json TEXT NOT NULL,
    original_conflicts_json TEXT NOT NULL,
    original_overall_confidence_bps INTEGER NOT NULL,
    effective_raw_json TEXT NOT NULL,
    effective_normalized_json TEXT NOT NULL,
    corrected_fields_json TEXT NOT NULL,
    confirmed_fields_json TEXT NOT NULL,
    confirmed_distinct_ids_json TEXT NOT NULL,
    issues_json TEXT NOT NULL,
    logical_key TEXT,
    status TEXT NOT NULL CHECK (status IN ({_STATUSES})),
    {_INITIAL_STATUS_COLUMN},
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    event_type TEXT NOT NULL CHECK (event_type IN ({_EVENT_TYPES})),
    reviewer_label TEXT NOT NULL,
    reason TEXT,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidates_logical_key ON candidates(logical_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_one_in_flight
    ON extraction_attempts(source_document_id) WHERE status = 'started';
CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_one_success
    ON extraction_attempts(source_document_id) WHERE status = 'succeeded';
CREATE INDEX IF NOT EXISTS idx_review_events_candidate ON review_events(candidate_id, id);

CREATE TRIGGER IF NOT EXISTS review_events_no_update
BEFORE UPDATE ON review_events
BEGIN SELECT RAISE(ABORT, 'review events are append-only'); END;

CREATE TRIGGER IF NOT EXISTS review_events_no_delete
BEFORE DELETE ON review_events
BEGIN SELECT RAISE(ABORT, 'review events are append-only'); END;

CREATE TRIGGER IF NOT EXISTS candidates_original_evidence_immutable
BEFORE UPDATE ON candidates
WHEN NEW.original_raw_json IS NOT OLD.original_raw_json
  OR NEW.original_normalized_json IS NOT OLD.original_normalized_json
  OR NEW.original_field_confidence_json IS NOT OLD.original_field_confidence_json
  OR NEW.original_conflicts_json IS NOT OLD.original_conflicts_json
  OR NEW.original_overall_confidence_bps IS NOT OLD.original_overall_confidence_bps
  OR NEW.initial_status IS NOT OLD.initial_status
BEGIN SELECT RAISE(ABORT, 'original evidence is immutable'); END;

CREATE TRIGGER IF NOT EXISTS candidates_no_delete
BEFORE DELETE ON candidates
BEGIN SELECT RAISE(ABORT, 'candidates cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS attempts_terminal_immutable
BEFORE UPDATE ON extraction_attempts
WHEN OLD.status <> 'started'
BEGIN SELECT RAISE(ABORT, 'terminal attempts are immutable'); END;

CREATE TRIGGER IF NOT EXISTS attempts_none_after_success
BEFORE INSERT ON extraction_attempts
WHEN EXISTS (SELECT 1 FROM extraction_attempts
             WHERE source_document_id = NEW.source_document_id AND status = 'succeeded')
BEGIN SELECT RAISE(ABORT, 'a succeeded source cannot be extracted again'); END;

CREATE TRIGGER IF NOT EXISTS attempts_no_delete
BEFORE DELETE ON extraction_attempts
BEGIN SELECT RAISE(ABORT, 'attempts cannot be deleted'); END;
"""

# Upgrade from the one-attempt-per-source schema (SQLite's documented table-rebuild procedure).
# Every legacy attempt becomes attempt 1; legacy candidates keep ``initial_status`` NULL because
# their routing at creation was never recorded and must not be guessed.
_MIGRATE_TO_V2 = f"""
BEGIN IMMEDIATE;
DROP TRIGGER IF EXISTS attempts_terminal_immutable;
DROP TRIGGER IF EXISTS attempts_no_delete;
DROP TRIGGER IF EXISTS candidates_original_evidence_immutable;
{_attempts_table("extraction_attempts_new", if_not_exists=False)}
INSERT INTO extraction_attempts_new (id, source_document_id, attempt_number, method, status,
    error_code, raw_json, field_confidence_json, conflicts_json, created_at, finished_at)
SELECT id, source_document_id, 1, method, status, error_code, raw_json, field_confidence_json,
    conflicts_json, created_at, finished_at FROM extraction_attempts;
DROP TABLE extraction_attempts;
ALTER TABLE extraction_attempts_new RENAME TO extraction_attempts;
ALTER TABLE candidates ADD COLUMN {_INITIAL_STATUS_COLUMN};
{_SCHEMA}
PRAGMA user_version = {SCHEMA_VERSION};
"""


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Database:
    def __init__(
        self,
        path: Path | str | None = None,
        *,
        now_provider: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._path = Path(path) if path is not None else get_db_path()
        self._now = now_provider

    # ------------------------------------------------------------ connections

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived connection with foreign keys on; closes it on exit."""
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        connection.isolation_level = None
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    def _timestamp(self) -> str:
        return self._now().astimezone(UTC).isoformat()

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            if _needs_v2_upgrade(connection):
                self._upgrade_to_v2(connection)
            else:
                connection.executescript(_SCHEMA)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _upgrade_to_v2(connection: sqlite3.Connection) -> None:
        """All-or-nothing: on any failure the legacy tables, triggers and rows are untouched."""
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.executescript(_MIGRATE_TO_V2)  # leaves its transaction open
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise sqlite3.IntegrityError("foreign key check failed after upgrade")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    # ---------------------------------------------------------------- sources

    def get_or_create_source_document(
        self, sha256: str, display_filename: str, size_bytes: int
    ) -> tuple[SourceDocumentRecord, bool]:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM source_documents WHERE sha256 = ?", (sha256,)
            ).fetchone()
            created = row is None
            if created:
                connection.execute(
                    "INSERT INTO source_documents (id, sha256, display_filename, size_bytes, "
                    "duplicate_count, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                    (uuid.uuid4().hex, sha256, display_filename, size_bytes, self._timestamp()),
                )
                row = connection.execute(
                    "SELECT * FROM source_documents WHERE sha256 = ?", (sha256,)
                ).fetchone()
        return _source_from_row(row), created

    def get_source_document_by_sha256(self, sha256: str) -> SourceDocumentRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_documents WHERE sha256 = ?", (sha256,)
            ).fetchone()
        return _source_from_row(row) if row else None

    def record_duplicate_observation(self, source_document_id: str) -> int:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE source_documents SET duplicate_count = duplicate_count + 1 WHERE id = ?",
                (source_document_id,),
            )
            row = connection.execute(
                "SELECT duplicate_count FROM source_documents WHERE id = ?",
                (source_document_id,),
            ).fetchone()
        return int(row["duplicate_count"])

    # --------------------------------------------------------------- attempts

    def create_attempt(self, source_document_id: str, method: str) -> AttemptRecord:
        """Start the next numbered attempt.

        The schema allows at most one in-flight and one succeeded attempt per source, so this
        raises ``sqlite3.IntegrityError`` while an attempt is running or after a success.
        """
        with self._transaction() as connection:
            return self._insert_attempt(connection, source_document_id, method)

    def retry_attempt(self, source_document_id: str, method: str) -> AttemptRecord:
        """Explicit retry: keep every earlier attempt and start the next one.

        A stranded ``started`` attempt is preserved as an ``extraction_interrupted`` error.
        Raises ``RetryNotAllowed`` for an unknown source or one that already succeeded.
        """
        with self._transaction() as connection:
            known = connection.execute(
                "SELECT 1 FROM source_documents WHERE id = ?", (source_document_id,)
            ).fetchone()
            if known is None:
                raise RetryNotAllowed("unknown_document")
            latest = connection.execute(
                "SELECT id, status FROM extraction_attempts WHERE source_document_id = ? "
                "ORDER BY attempt_number DESC LIMIT 1",
                (source_document_id,),
            ).fetchone()
            if latest is not None and latest["status"] == AttemptStatus.SUCCEEDED.value:
                raise RetryNotAllowed("already_succeeded")
            if latest is not None and latest["status"] == AttemptStatus.STARTED.value:
                connection.execute(
                    "UPDATE extraction_attempts SET status = 'error', "
                    "error_code = 'extraction_interrupted', finished_at = ? WHERE id = ?",
                    (self._timestamp(), latest["id"]),
                )
            return self._insert_attempt(connection, source_document_id, method)

    def _insert_attempt(
        self, connection: sqlite3.Connection, source_document_id: str, method: str
    ) -> AttemptRecord:
        attempt_id = uuid.uuid4().hex
        number = connection.execute(
            "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM extraction_attempts "
            "WHERE source_document_id = ?",
            (source_document_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO extraction_attempts (id, source_document_id, attempt_number, method, "
            "status, created_at) VALUES (?, ?, ?, ?, 'started', ?)",
            (attempt_id, source_document_id, number, method, self._timestamp()),
        )
        row = connection.execute(
            "SELECT * FROM extraction_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        return _attempt_from_row(row)

    def finish_attempt_success(
        self,
        attempt_id: str,
        raw: RawInvoiceFields,
        field_confidence_bps: dict[str, int],
        conflicts: list[FieldConflict],
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE extraction_attempts SET status = 'succeeded', raw_json = ?, "
                "field_confidence_json = ?, conflicts_json = ?, finished_at = ? WHERE id = ?",
                (
                    _dumps(raw.to_json_dict()),
                    _dumps(field_confidence_bps),
                    _dumps([c.model_dump() for c in conflicts]),
                    self._timestamp(),
                    attempt_id,
                ),
            )

    def finish_attempt_error(self, attempt_id: str, error_code: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE extraction_attempts SET status = 'error', error_code = ?, "
                "finished_at = ? WHERE id = ?",
                (error_code, self._timestamp(), attempt_id),
            )

    def get_attempt_for_source(self, source_document_id: str) -> AttemptRecord | None:
        """The latest attempt for the source, if any."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM extraction_attempts WHERE source_document_id = ? "
                "ORDER BY attempt_number DESC LIMIT 1",
                (source_document_id,),
            ).fetchone()
        return _attempt_from_row(row) if row else None

    def list_attempts_for_source(self, source_document_id: str) -> list[AttemptRecord]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM extraction_attempts WHERE source_document_id = ? "
                "ORDER BY attempt_number",
                (source_document_id,),
            ).fetchall()
        return [_attempt_from_row(row) for row in rows]

    # ------------------------------------------------------------- candidates

    def create_candidate(
        self,
        *,
        attempt_id: str,
        original_raw: RawInvoiceFields,
        original_normalized: NormalizedInvoiceFields,
        original_field_confidence_bps: dict[str, int],
        original_overall_confidence_bps: int,
        conflicts: list[FieldConflict],
        issues: list[ValidationIssue],
        status: CandidateStatus,
        logical_key: str | None,
    ) -> InvoiceCandidate:
        candidate_id = uuid.uuid4().hex
        now = self._timestamp()
        raw_json = _dumps(original_raw.to_json_dict())
        normalized_json = _dumps(original_normalized.to_json_dict())
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO candidates (id, attempt_id, original_raw_json, "
                "original_normalized_json, original_field_confidence_json, "
                "original_conflicts_json, original_overall_confidence_bps, effective_raw_json, "
                "effective_normalized_json, corrected_fields_json, confirmed_fields_json, "
                "confirmed_distinct_ids_json, issues_json, logical_key, status, initial_status, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', '[]', '[]', ?, ?, ?, ?, ?, ?)",
                (
                    candidate_id,
                    attempt_id,
                    raw_json,
                    normalized_json,
                    _dumps(original_field_confidence_bps),
                    _dumps([c.model_dump() for c in conflicts]),
                    original_overall_confidence_bps,
                    raw_json,
                    normalized_json,
                    _dumps([i.model_dump(mode="json") for i in issues]),
                    logical_key,
                    status.value,
                    status.value,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        return _candidate_from_row(row)

    def get_candidate(self, candidate_id: str) -> InvoiceCandidate | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        return _candidate_from_row(row) if row else None

    def get_candidate_for_attempt(self, attempt_id: str) -> InvoiceCandidate | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        return _candidate_from_row(row) if row else None

    def list_candidates(self, status: CandidateStatus | None = None) -> list[InvoiceCandidate]:
        query = "SELECT * FROM candidates"
        params: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status.value,)
        with self.connect() as connection:
            rows = connection.execute(query + " ORDER BY seq", params).fetchall()
        return [_candidate_from_row(row) for row in rows]

    def find_logical_duplicates(
        self, logical_key: str | None, *, exclude_candidate_id: str
    ) -> list[str]:
        """Sorted IDs of other candidates with this key; rejected candidates never match."""
        if logical_key is None:
            return []
        placeholders = ", ".join("?" for _ in _OPEN_STATUSES)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT id FROM candidates WHERE logical_key = ? AND id <> ? "
                f"AND status IN ({placeholders})",
                (logical_key, exclude_candidate_id, *_OPEN_STATUSES),
            ).fetchall()
        return sorted(row["id"] for row in rows)

    def apply_review_change_with_event(
        self,
        candidate_id: str,
        *,
        effective_raw: RawInvoiceFields,
        effective_normalized: NormalizedInvoiceFields,
        corrected_fields: set[str],
        confirmed_fields: set[str],
        confirmed_distinct_candidate_ids: set[str],
        issues: list[ValidationIssue],
        status: CandidateStatus,
        logical_key: str | None,
        event_type: ReviewEventType,
        reviewer_label: str,
        reason: str | None,
        details: dict[str, Any],
    ) -> InvoiceCandidate:
        """The only mutation after insertion: effective state plus one event, atomically."""
        now = self._timestamp()
        with self._transaction() as connection:
            connection.execute(
                "UPDATE candidates SET effective_raw_json = ?, effective_normalized_json = ?, "
                "corrected_fields_json = ?, confirmed_fields_json = ?, "
                "confirmed_distinct_ids_json = ?, issues_json = ?, logical_key = ?, "
                "status = ?, updated_at = ? WHERE id = ?",
                (
                    _dumps(effective_raw.to_json_dict()),
                    _dumps(effective_normalized.to_json_dict()),
                    _dumps(sorted(corrected_fields)),
                    _dumps(sorted(confirmed_fields)),
                    _dumps(sorted(confirmed_distinct_candidate_ids)),
                    _dumps([i.model_dump(mode="json") for i in issues]),
                    logical_key,
                    status.value,
                    now,
                    candidate_id,
                ),
            )
            connection.execute(
                "INSERT INTO review_events (candidate_id, event_type, reviewer_label, reason, "
                "details_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    candidate_id,
                    getattr(event_type, "value", event_type),
                    reviewer_label,
                    reason,
                    _dumps(details),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        return _candidate_from_row(row)

    def list_review_events(self, candidate_id: str) -> list[ReviewEvent]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM review_events WHERE candidate_id = ? ORDER BY id",
                (candidate_id,),
            ).fetchall()
        return [
            ReviewEvent(
                id=row["id"],
                candidate_id=row["candidate_id"],
                event_type=ReviewEventType(row["event_type"]),
                reviewer_label=row["reviewer_label"],
                reason=row["reason"],
                details=json.loads(row["details_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # ---------------------------------------------------------------- metrics

    def metrics_snapshot(self) -> dict[str, Any]:
        with self.connect() as connection:
            sources, duplicates = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(duplicate_count), 0) FROM source_documents"
            ).fetchone()
            attempts, errors, retries = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(status = 'error'), 0), "
                "COALESCE(SUM(attempt_number > 1), 0) FROM extraction_attempts"
            ).fetchone()
            counts = {s.value: 0 for s in CandidateStatus}
            for row in connection.execute(
                "SELECT status, COUNT(*) AS n FROM candidates GROUP BY 1"
            ):
                counts[row["status"]] = row["n"]
            initial_counts = {
                CandidateStatus.AUTO_READY.value: 0,
                CandidateStatus.REVIEW_REQUIRED.value: 0,
            }
            unknown_initial = 0
            for row in connection.execute(
                "SELECT initial_status, COUNT(*) AS n FROM candidates GROUP BY 1"
            ):
                if row["initial_status"] is None:
                    unknown_initial = row["n"]
                else:
                    initial_counts[row["initial_status"]] = row["n"]
            average = connection.execute(
                "SELECT AVG(original_overall_confidence_bps) FROM candidates"
            ).fetchone()[0]
            issue_rows = connection.execute("SELECT issues_json FROM candidates").fetchall()
        flagged = sum(
            1
            for row in issue_rows
            if any(
                issue["code"] == IssueCode.POSSIBLE_LOGICAL_DUPLICATE.value
                for issue in json.loads(row["issues_json"])
            )
        )
        return {
            "sources": sources,
            "attempts": attempts,
            "extraction_errors": errors,
            "retry_attempts": retries,
            "content_duplicates": duplicates,
            "successful_candidates": sum(counts.values()),
            "logical_duplicate_flags": flagged,
            "status_counts": counts,
            "initial_status_counts": initial_counts,
            "initial_routing_unknown": unknown_initial,
            "average_original_confidence_bps": None if average is None else int(average),
        }


def _needs_v2_upgrade(connection: sqlite3.Connection) -> bool:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(extraction_attempts)")}
    return bool(columns) and "attempt_number" not in columns


def _source_from_row(row: sqlite3.Row) -> SourceDocumentRecord:
    return SourceDocumentRecord(
        id=row["id"],
        sha256=row["sha256"],
        display_filename=row["display_filename"],
        size_bytes=row["size_bytes"],
        duplicate_count=row["duplicate_count"],
        created_at=row["created_at"],
    )


def _conflicts(text: str | None) -> list[FieldConflict]:
    return [FieldConflict(**item) for item in json.loads(text)] if text else []


def _attempt_from_row(row: sqlite3.Row) -> AttemptRecord:
    return AttemptRecord(
        id=row["id"],
        source_document_id=row["source_document_id"],
        attempt_number=row["attempt_number"],
        method=row["method"],
        status=AttemptStatus(row["status"]),
        error_code=row["error_code"],
        raw=RawInvoiceFields(**json.loads(row["raw_json"])) if row["raw_json"] else None,
        field_confidence_bps=json.loads(row["field_confidence_json"] or "{}"),
        conflicts=_conflicts(row["conflicts_json"]),
        created_at=row["created_at"],
    )


def _candidate_from_row(row: sqlite3.Row) -> InvoiceCandidate:
    return InvoiceCandidate(
        id=row["id"],
        attempt_id=row["attempt_id"],
        original_raw=RawInvoiceFields(**json.loads(row["original_raw_json"])),
        original_normalized=NormalizedInvoiceFields.from_json_dict(
            json.loads(row["original_normalized_json"])
        ),
        original_field_confidence_bps=json.loads(row["original_field_confidence_json"]),
        original_overall_confidence_bps=row["original_overall_confidence_bps"],
        conflicts=_conflicts(row["original_conflicts_json"]),
        effective_raw=RawInvoiceFields(**json.loads(row["effective_raw_json"])),
        effective_normalized=NormalizedInvoiceFields.from_json_dict(
            json.loads(row["effective_normalized_json"])
        ),
        corrected_fields=set(json.loads(row["corrected_fields_json"])),
        confirmed_fields=set(json.loads(row["confirmed_fields_json"])),
        confirmed_distinct_candidate_ids=set(json.loads(row["confirmed_distinct_ids_json"])),
        issues=[ValidationIssue(**item) for item in json.loads(row["issues_json"])],
        status=CandidateStatus(row["status"]),
        initial_status=(
            CandidateStatus(row["initial_status"]) if row["initial_status"] is not None else None
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
