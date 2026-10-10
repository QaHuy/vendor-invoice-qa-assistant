# Security notes

This is a synthetic-data portfolio demo, not production software. Do not upload real
invoices or any customer, supplier, employee or financial data.

## Data and secrets

- Use synthetic documents and fictional organizations only.
- No API keys, credentials or `.env` files belong in this repository. `.env.example` holds the
  single non-secret variable `INVOICE_QA_DB_PATH`. Configuration comes from the process
  environment; there is no dotenv loader.
- Uploaded PDFs, generated databases, evaluation output, logs and generated fixtures are
  gitignored.

## PDF handling

Checks run in this order and stop at the first failure: empty content, `.pdf` extension, size
(at most 5 MiB), `%PDF-` magic bytes, structural parse with `pypdf`, encryption, page count.
The size limit is enforced three times: the Streamlit widget (`max_upload_size=5`),
`.streamlit/config.toml` (`maxUploadSize = 5`) and the application (`5 * 1024 * 1024` bytes,
checked before any parsing). PDFs are parsed in memory; embedded content is never executed;
filenames are sanitized for display and never passed to a shell. A valid PDF without a
text layer is not a security rejection; it records an extraction error (`no_extractable_text`).

## Retention

Only the SHA-256, sanitized display filename, byte size, raw extracted field values,
normalized values, confidence, issues and review events are stored in SQLite. PDF bytes and
full extracted text are never stored or logged.

## Sensitive fields

Vendor tax IDs are masked (final four characters at most) in the queue, metrics, events and
exports. The edit form reveals the synthetic value only after an explicit, session-only
"Edit tax ID" action; the reveal state is never persisted.

## Error handling

UI messages are generic. Internal exception details are kept out of the UI, metrics and
persisted error codes. AI-boundary errors carry stable codes only, never source text or
provider payloads.

## Audit integrity

Review events are append-only (SQLite triggers abort `UPDATE` and `DELETE`). Original raw
values, original normalized values and original confidence cannot be changed after insertion.
This protects against accidental edits by the application, not against someone with direct
write access to the database file.

## Dependencies

`streamlit`, `pypdf`, `pydantic`, `pandas` (version ranges, no lock file) and `reportlab`
(pinned exactly, because fixture bytes depend on it); `pytest` and `ruff` for development. No real-model SDK, HTTP client or
other PDF library is declared. The default operation is local and network-free.

## Known limitations

No authentication or authorization, no encryption at rest, no rate limiting, no malware
scanning, no retention or deletion workflow, free-text reviewer reasons are stored as typed,
and rejected uploads are not counted in persisted metrics.
