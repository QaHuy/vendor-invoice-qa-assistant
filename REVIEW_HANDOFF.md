# Review handoff

Synthetic-data portfolio MVP. Not production software. Nothing was pushed, published or
deployed; the repository has no remote.

- **Baselines:** original evaluation commit `c8b89c3`; retry + metrics split `04bf562`
  (documented by `a80f153`); retry-integrity fixes (atomic success, no early interruption,
  recovery, no-text sample) are in the commit that follows `a80f153`. The final commit SHA is
  reported outside this file after committing.
- Verified with Python **3.14.6** (primary `.venv`) and **3.11.15** (uv-managed, installed with
  `uv python install 3.11 --no-bin --no-registry`, venv kept outside the repository) on
  Windows 11. `requires-python` is `>=3.11`; Ruff targets `py311`. An earlier run of the
  previous revision on 3.12.13 is not repeated here.
- Linter versions differ between the two environments: the 3.14.6 environment used **Ruff
  0.16.10**, the 3.11.15 environment used **Ruff 0.17.0** (`pyproject.toml` only requires
  `ruff>=0.6`, so each fresh environment resolved the newest release).
- Dependency pinning: only `reportlab==5.0.1` is pinned exactly in `pyproject.toml` (fixture
  bytes depend on it). The other dependencies are version ranges (`streamlit>=1.50,<2`,
  `pypdf>=6,<7`, `pydantic>=2,<3`, `pandas>=2.2,<4`, `pytest>=8`, `ruff>=0.6`) and there is no
  lock file. Versions resolved in both environments: streamlit 1.65.0, pypdf 6.20.0, reportlab
  5.0.1, pydantic 2.14.0, pandas 3.0.6, pytest 9.1.1.
- **Independent review:** an independent read-only review was completed after commit `05850f1`
  and reran: Python 3.14.6 - 334 tests passed, Ruff passed, evaluator passed; Python 3.11.15 -
  334 tests passed, Ruff passed, evaluator passed (evaluator: 13/13 scenarios, 111/111 fields,
  `initial_review_rate` 0.7500, `false_auto_ready` 0, `false_review` 0). It found **no new P0/P1
  blocker** in the atomic-success, retry-concurrency, orphan-recovery or no-text-sample changes.
  Its non-blocking findings are kept in the limitations below.

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

Second follow-up (retry integrity): `Database.complete_attempt_with_candidate()` stores the
successful extraction and creates the candidate (with its immutable `initial_status`) in one
transaction, replacing the old two-step `finish_attempt_success` + `create_candidate`;
`retry_attempt()` refuses to touch a `started` attempt younger than `STALE_ATTEMPT_AFTER`
(10 minutes, injected clock) and the UI shows a generic "already in progress" message; a
succeeded attempt without a candidate is repaired from its stored raw evidence; the sample
generator writes `retry-demo-no-text.pdf`.

## Plan alignment (retry)

The original plan stated that retry was out of scope and that each source had exactly one
attempt. At the project owner's request the implementation added explicit manual retry, and the
plan file (kept outside this repository and not tracked) was then updated by the owner's
instruction so that it describes the implemented behaviour: several immutable numbered attempts
per source, at most one `started` and one `succeeded` attempt, `attempt_in_progress` for a
recent attempt, the 10-minute stale threshold, repair of a succeeded attempt without a
candidate from stored evidence, atomic completion, the `initial_review_rate` /
`current_review_backlog_share` split, and `retry-demo-no-text.pdf`. Automatic retry, backoff and
retry limits remain out of scope. The plan and the implementation therefore no longer conflict
on retry.

## Omitted scope (by design)

Real model/API client, automatic extraction retry/backoff, amount rounding, email ingestion,
ERP/payment integrations, authentication, OCR, reopen of terminal candidates, counting security-rejected
uploads in persisted metrics.

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
# second follow-up: isolated Python 3.11 (no PATH, default-Python or registry change)
uv python install 3.11 --no-bin --no-registry
uv venv <outside-repo-dir>/venv311 --python 3.11
uv pip install --python <outside-repo-dir>/venv311/Scripts/python.exe -e ".[dev]"
<outside-repo-dir>/venv311/Scripts/python.exe -m pytest -q
<outside-repo-dir>/venv311/Scripts/ruff.exe check .
<outside-repo-dir>/venv311/Scripts/python.exe scripts/evaluate.py --manifest sample_data/manifest.json --output .evaluation/report.json
```

## Results

- `ruff check .`: all checks passed on both interpreters.
- `pytest -q`: **334 passed**, 0 failed, 0 skipped on Python 3.14.6; **334 passed** on 3.11.15.
- `git diff --check`: no output (clean).
- Generator: 14 PDFs (13 fixtures + `retry-demo-no-text.pdf`) written to
  `sample_data/generated/` (gitignored); two generations had identical bytes per file (pinned
  by `tests/test_sample_data.py`), and the 3.11 and 3.14 outputs were byte-identical (checked
  once with `cmp`, not pinned by a test).
- Evaluator exit code 0 on both 3.11.15 and 3.14.6, identical result:

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
6. **Final review** of the original eight tasks was a self-review by the implementing agent
   (no subagents allowed by the run instructions). The retry-integrity follow-up (`05850f1`)
   received an independent read-only review, summarised in the header; the original tasks have
   not had a separate independent review.
7. The Task 5 and Task 7 commits were amended locally (unpushed): Task 5 to include a lint
   fix, Task 7 to replace a hostile-filename test vector that looked like an absolute path.
8. **Retry (follow-up, overrides the plan):** retry needs the file again because PDF bytes are
   never stored. A retry of a *stale* `started` attempt marks it `extraction_interrupted`
   (preserved) before starting the next one; a recent one returns `attempt_in_progress` and is
   not modified (ruling 11). A source that crashed before its first attempt starts attempt 1 on
   retry. A succeeded source never gets another attempt (DB trigger + partial unique indexes);
   retrying it just returns its candidate.
9. **Initial routing (follow-up):** `candidates.initial_status` is immutable (trigger). Rows
   from before this column existed keep `NULL` (their routing was never recorded and is not
   guessed) and are excluded from `initial_review_rate`; `metrics` reports their count as
   `initial_routing_unknown`. The old `review_rate` key is removed everywhere, including the
   evaluator report (now `initial_review_rate`). Cost if wrong: external readers of the old key.
10. **Schema upgrade (follow-up):** databases created by the one-attempt schema are rebuilt in
    place in one transaction (SQLite's documented table-rebuild procedure, foreign keys off,
    `foreign_key_check` before commit, rollback on any failure). Verified on a legacy-schema
    fixture only; there is no real legacy database to test against.

11. **Stale-attempt threshold (second follow-up):** `STALE_ATTEMPT_AFTER = 10 minutes`, a
    boundary-inclusive comparison against the injected database clock, configurable per
    `Database`. Chosen conservatively because a size-capped deterministic extraction takes
    seconds; it is not measured. Cost if wrong: a crashed attempt blocks retry for up to 10
    minutes, or an extraction slower than 10 minutes could be interrupted.
12. **Atomic success (second follow-up):** the public two-step `finish_attempt_success` and
    `create_candidate` were removed so the split cannot be reintroduced by a caller; validation
    and duplicate lookup still run before the transaction, so a concurrent logical duplicate
    created between lookup and commit is not seen (unchanged from before). If the transaction
    fails for any reason the attempt is recorded as an error with the safe code
    `candidate_creation_failed` and is retryable. Cost if wrong: a failure inside the
    transaction is reported generically and the exception detail is discarded.
13. **Recovery of an orphaned success (second follow-up):** automatic on the next upload or
    retry of that file (no PDF needed, no new attempt), rebuilding the candidate from the stored
    `raw_json`, confidence and conflicts. Validation uses the date and duplicates at recovery
    time, so the recovered status can differ from what the original run would have produced.
    No real orphan data exists; recovery is verified on a synthetic orphan only.
14. **Sample generator (second follow-up):** `retry-demo-no-text.pdf` is built by
    `invoice_qa.synthetic.build_no_text_pdf()` and written by `generate_samples.py` but is not
    in `manifest.json`, so the evaluator stays at 13 scenarios.

## Known bugs and limitations

- See README section 13. In addition: the Streamlit pages are covered by AppTest (including
  real uploads and the retry button), and one scripted browser smoke test was run (see
  "Browser smoke test" below). No human has used the app by hand in a browser yet; the owner
  should do one manual pass. An earlier version of this file said AppTest cannot simulate
  uploads; that was wrong for Streamlit 1.65 (`FileUploader.set_value`).
- The suite was run on 3.14.6 and 3.11.15 only (not 3.12/3.13 for this revision).
- Streamlit sessions share one SQLite file; concurrent writers are serialized by SQLite
  (`BEGIN IMMEDIATE`). The concurrency test uses two `Database`/`IntakeService` instances and
  two threads on one file, not two browser sessions.
- The "already in progress" message is cached per uploaded file in a session; the user must
  remove and re-upload the file to see the result (a refresh button was not added).
- The stale threshold (10 minutes) is a documented guess; a crashed attempt blocks retry until
  it expires, and an extraction slower than the threshold could be interrupted.
- Orphan-attempt repair and the legacy-database upgrade were verified on synthetic fixtures
  only; no real legacy database exists to test against.
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

## Browser smoke test

One scripted run (not a manual session): headless Google Chrome driven by Playwright 1.63.0,
which was installed in a throwaway virtual environment outside the repository. The app was
started from the repository's own environment on `127.0.0.1` with a database in a temporary
directory and usage statistics off, using only the generated synthetic PDFs, then stopped. The
flows were executed in two script parts against the same database, and the first part was
re-run once after a locator problem, so the sequence below is the verified outcome rather than
one uninterrupted run:

- clean invoice upload -> `auto_ready`;
- malformed-amount invoice upload -> `review_required`;
- `retry-demo-no-text.pdf` upload -> generic extraction error and a **Retry extraction**
  button, with no attempt created until the click;
- one manual **Retry extraction** click -> a second immutable `error` attempt (attempt 2), the
  first kept;
- Review Queue: correction of `Subtotal` to `1,000.00` turned the candidate `auto_ready`
  (initial routing stayed `review_required`); both candidates approved;
- approved CSV download: 2 rows, vendor tax IDs masked (no full `DEMO-TAX-` ID present);
- Metrics page: initial review rate 50 % (unchanged by approvals), current review backlog
  share 0 %, 1 retry attempt, 2 failed attempts.

Not exercised in the browser: reject, confirm-low-confidence, confirm-distinct, JSON download,
the stale-attempt path, and the "already in progress" message (these are covered by AppTest or
service tests).

## Screenshots

`screenshots/` holds eight PNGs captured from that run (synthetic data only; no local paths,
e-mail addresses or secrets are visible; the Streamlit "Deploy" toolbar of the local dev server
is visible):
`01-upload-clean-invoice`, `02-upload-review-required`, `03-extraction-error-with-retry`,
`04-after-manual-retry`, `05-review-queue`, `06-review-required-candidate`,
`07-approved-and-export`, `08-metrics`.
