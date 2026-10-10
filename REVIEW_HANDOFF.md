# Review handoff

Synthetic-data portfolio MVP. Not production software. Nothing was pushed, published or
deployed; the repository has no remote.

- **Code-baseline commit** (last code commit, before the documentation commit): `04bf562`
  (follow-up: explicit retry and split review-rate metrics; the original evaluation commit
  was `c8b89c3`)
- The final documentation commit SHA is reported outside this file after committing.
- Verified with Python 3.14.x (primary `.venv`) and 3.12.13 (separate out-of-repo venv) on
  Windows 11. `requires-python` is `>=3.11`; Ruff targets `py311`; all sources parse with the
  3.11 grammar. **Blocker: no Python 3.11 interpreter is installed, and installing one needs a
  download outside the allowed network scope (PyPI + official docs), so the suite has NOT been
  executed on 3.11.** To resolve: `uv python install 3.11`, `uv venv --python 3.11 <dir>`,
  install `-e ".[dev]"` and run `pytest -q`.
- Pinned/resolved versions used: streamlit 1.65.0, pypdf 6.20.0, reportlab 5.0.1 (exact pin),
  pydantic 2.14.0, pandas 3.0.6, pytest 9.1.1, ruff 0.16.10.

## Implemented scope

Tasks 1-8 of the plan: scaffold and configuration; domain (raw/normalized models, exact
normalization, confidence, validation, transitions); deterministic PDF factory, file security,
deterministic extraction, AI protocol + validation boundary + test-only fake provider; SQLite
storage with append-only/immutability triggers; intake, review, metrics and export services;
four-page Streamlit UI; reproducible 13-scenario evaluation; README, SECURITY.md.

Follow-up (commit `04bf562`, requested after the original run): explicit extraction retry
(several immutable numbered attempts per source, in-place upgrade of older databases, a
**Retry extraction** button) and `initial_review_rate` vs. `current_review_backlog_share`
(initial routing persisted on each candidate).

## Deviation from the plan (intentional, requested by the project owner)

The plan file (`docs/superpowers/plans/...-v2.md`, not edited) states that retry is out of scope
and that each source has exactly one attempt (lines 35, 48, 280, 333, 507, 1471, and the
`UNIQUE(source_document_id)` test at ~925). The follow-up request overrode that, so the
implementation now **differs from the plan** on exactly this point; README, About page and tests
describe the new behaviour. The plan was not changed to match.

## Omitted scope (by design)

Real model/API client, automatic extraction retry/backoff, amount rounding, email ingestion,
ERP/payment integrations, authentication, OCR, reopen of terminal candidates, counting security-rejected
uploads in persisted metrics, screenshots.

## Exact commands run (from the repository root, inside `.venv`)

```bash
python -m venv .venv                       # Python 3.14
.venv/Scripts/python -m pip install -e ".[dev]"
ruff check .
pytest -q
python sample_data/generate_samples.py
python scripts/evaluate.py --manifest sample_data/manifest.json --output .evaluation/report.json
git diff --check
git status --short
# follow-up only: the same suite on Python 3.12 from a venv kept outside the repository
uv venv <outside-repo-dir>/venv312 --python 3.12
uv pip install --python <outside-repo-dir>/venv312/Scripts/python.exe -e ".[dev]"
<outside-repo-dir>/venv312/Scripts/python.exe -m pytest -q
```

## Results

- `ruff check .`: all checks passed.
- `pytest -q`: **308 passed**, 0 failed, 0 skipped on Python 3.14; **308 passed** on 3.12.13.
- `git diff --check`: no output (clean).
- Generator: 13 PDFs written to `sample_data/generated/` (gitignored); two generations had
  identical SHA-256 per fixture (pinned by `tests/test_sample_data.py`).
- Evaluator exit code 0:

| Metric | Result |
|---|---|
| Scenarios matching manifest | 13 / 13 |
| Field accuracy | 111 / 111 |
| Initial review rate | 0.7500 (9 of 12 new candidates) |
| False auto-ready | 0 |
| False review | 0 |

These are synthetic, local, regression-style numbers; the expectations were authored together
with the rules, so they do not indicate real-world accuracy.

## README claims that depend on measured evidence

| README claim | Evidence |
|---|---|
| 13/13 scenarios, 111/111 fields, initial review rate 0.75, 0 false auto-ready, 0 false review | `scripts/evaluate.py` output above; `.evaluation/report.json` (gitignored) |
| 13 fixtures, byte-reproducible | `tests/test_sample_data.py::test_two_generations_have_identical_hashes` |
| 5 MiB accepted / +1 byte rejected, oversized rejected before parsing | `tests/test_file_security.py`, `tests/test_project_config.py`, `tests/test_app.py::test_upload_control_renders_for_pdf_input` |
| Overall-confidence warning fires rarely (worked examples 9642 / 8571 / 8428) | `tests/test_domain.py::test_worked_confidence_examples` |

## Rulings and deviations from the plan

1. **`ReviewService.recheck()` added** (not in the plan's interface list). Without it, a
   candidate downgraded by `approval_blocked` could never return to `auto_ready` after the
   newer duplicate is rejected, contradicting "approving A is also blocked until B is
   rejected". It re-runs validation and appends a `confirmed` event with
   `{"recheck": true, "issue_codes": [...]}`. Cost if wrong: one extra public method.
2. **`Database.get_candidate_for_attempt()`, `logical_duplicate_key()`, `fixture_pdf()`,
   record models (`InvoiceCandidate`, `ReviewEvent`, ...) added** as supporting helpers the
   plan's interfaces implied but did not name.
3. **Stranded `started` attempt** is reported as the safe code `extraction_interrupted`.
4. **CSV formula neutralization** (leading `=`, `+`, `-`, `@` get an apostrophe prefix) added
   to the export; a small hardening not in the plan.
5. **Navigation** is a sidebar radio (Upload, Review Queue, Metrics, About) so AppTest can
   assert it.
6. **Final review** was a self-review by the implementing agent (no subagents allowed by the
   run instructions); an independent review is still required.
7. The Task 5 and Task 7 commits were amended locally (unpushed): Task 5 to include a lint
   fix, Task 7 to replace a hostile-filename test vector that looked like an absolute path.
8. **Retry (follow-up, overrides the plan):** retry needs the file again because PDF bytes are
   never stored. A retry of a stranded `started` attempt marks it `extraction_interrupted`
   (preserved) before starting the next one. A source that crashed before its first attempt
   starts attempt 1 on retry. A succeeded source never gets another attempt (DB trigger +
   partial unique indexes); retrying it just returns its candidate. Cost if wrong: two sessions
   retrying the same document at once interrupt each other's attempt.
9. **Initial routing (follow-up):** `candidates.initial_status` is immutable (trigger). Rows
   from before this column existed keep `NULL` (their routing was never recorded and is not
   guessed) and are excluded from `initial_review_rate`; `metrics` reports their count as
   `initial_routing_unknown`. The old `review_rate` key is removed everywhere, including the
   evaluator report (now `initial_review_rate`). Cost if wrong: external readers of the old key.
10. **Schema upgrade (follow-up):** databases created by the one-attempt schema are rebuilt in
    place in one transaction (SQLite's documented table-rebuild procedure, foreign keys off,
    `foreign_key_check` before commit, rollback on any failure). Verified on a legacy-schema
    fixture only; there is no real legacy database to test against.

## Known bugs and limitations

- See README section 13. In addition: the Streamlit pages were exercised through AppTest only
  (including uploads and the retry button), never manually in a browser. An earlier version of
  this file said AppTest cannot simulate uploads; that was wrong for Streamlit 1.65
  (`FileUploader.set_value`) and the UI tests now upload real PDFs.
- Python 3.11 was not available (see the header); only 3.14 and 3.12 were run.
- Streamlit sessions share one SQLite file; concurrent writers are serialized by SQLite
  (`BEGIN IMMEDIATE`) but there is no conflict UX.
- Line endings: Git on this machine warns that LF will become CRLF in the working copy
  (`core.autocrlf`); stored content is unaffected.

## Security and privacy decisions

PDF/size/encryption checks in a fixed order; generic UI errors; PDF bytes and full text never
persisted; vendor tax IDs masked everywhere except a session-only edit action; append-only
events and immutable original evidence enforced by SQLite triggers; no network use at run
time; no secrets in the repository. See SECURITY.md.

## Files requiring reviewer attention

- `src/invoice_qa/domain/normalization.py`, `validation.py` - exact money/date rules,
  issue priority, arithmetic skipping, confidence math.
- `src/invoice_qa/services/review.py` - revalidation after every action, approval-time
  duplicate recheck, `recheck()` ruling, event payload masking.
- `src/invoice_qa/infrastructure/database.py` - triggers, atomic change + event, duplicate
  lookup, connection handling.
- `src/invoice_qa/infrastructure/file_security.py` - check order and generic rejection.
- `src/invoice_qa/ui/review.py` - edit-field session synchronization, tax-ID reveal.
- `sample_data/manifest.json`, `scripts/evaluate.py` - ground truth and false-count logic.
- `README.md` evidence-dependent claims (table above).

## Screenshots

None produced (`screenshots/.gitkeep` only).
