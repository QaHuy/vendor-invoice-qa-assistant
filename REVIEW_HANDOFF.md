# Review handoff

Synthetic-data portfolio MVP. Not production software. Nothing was pushed, published or
deployed; the repository has no remote.

- **Code-baseline commit** (last code commit, before the documentation commit): `c8b89c3`
- The final documentation commit SHA is reported outside this file after committing.
- Verified with Python 3.14.x on Windows 11. `requires-python` is `>=3.11`; Ruff targets `py311`;
  all sources parse with the 3.11 grammar. No 3.11 interpreter was available, so the suite has
  **not** been executed on 3.11.
- Pinned/resolved versions used: streamlit 1.65.0, pypdf 6.20.0, reportlab 5.0.1 (exact pin),
  pydantic 2.14.0, pandas 3.0.6, pytest 9.1.1, ruff 0.16.10.

## Implemented scope

Tasks 1-8 of the plan: scaffold and configuration; domain (raw/normalized models, exact
normalization, confidence, validation, transitions); deterministic PDF factory, file security,
deterministic extraction, AI protocol + validation boundary + test-only fake provider; SQLite
storage with append-only/immutability triggers; intake, review, metrics and export services;
four-page Streamlit UI; reproducible 13-scenario evaluation; README, SECURITY.md.

## Omitted scope (by design)

Real model/API client, extraction retry, amount rounding, email ingestion, ERP/payment
integrations, authentication, OCR, reopen of terminal candidates, counting security-rejected
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
```

## Results

- `ruff check .`: all checks passed.
- `pytest -q`: **271 passed**, 0 failed, 0 skipped.
- `git diff --check`: no output (clean).
- Generator: 13 PDFs written to `sample_data/generated/` (gitignored); two generations had
  identical SHA-256 per fixture (pinned by `tests/test_sample_data.py`).
- Evaluator exit code 0:

| Metric | Result |
|---|---|
| Scenarios matching manifest | 13 / 13 |
| Field accuracy | 111 / 111 |
| Review rate | 0.7500 (9 of 12 new candidates) |
| False auto-ready | 0 |
| False review | 0 |

These are synthetic, local, regression-style numbers; the expectations were authored together
with the rules, so they do not indicate real-world accuracy.

## README claims that depend on measured evidence

| README claim | Evidence |
|---|---|
| 13/13 scenarios, 111/111 fields, review rate 0.75, 0 false auto-ready, 0 false review | `scripts/evaluate.py` output above; `.evaluation/report.json` (gitignored) |
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

## Known bugs and limitations

- See README section 13. In addition: AppTest cannot simulate file uploads, so upload flows are
  tested at the service layer; the Streamlit page itself was only exercised through AppTest,
  not manually in a browser.
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
