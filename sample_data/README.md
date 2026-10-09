# Synthetic evaluation data

Everything here is fictional. Organizations contain `Example` or `Demo`; identifiers start
with `DEMO-`, `INV-DEMO-` or `PO-DEMO-`. There are no person names, addresses, phone numbers,
emails or bank details.

- `manifest.json` — the committed ground truth: 13 fixtures in processing order, one
  `validation_reference_date` (the "today" used for the future-date check), and for each
  fixture the source lines, expected normalized values, expected overall confidence
  (basis points), the exact expected issue-code set, the expected initial status and any
  duplicate relationship.
- `generate_samples.py` — writes one PDF per fixture into `sample_data/generated/`
  (gitignored) using `invoice_qa.synthetic.build_invoice_pdf`. Output is byte-reproducible
  only for the exact pinned ReportLab version.
- `../scripts/evaluate.py` — processes the fixtures in manifest order against a fresh
  temporary database and compares actual results with the manifest.

```bash
python sample_data/generate_samples.py
python scripts/evaluate.py --manifest sample_data/manifest.json --output .evaluation/report.json
```

The evaluator exits non-zero if any scenario differs. Results describe this synthetic set
on this machine only; they are not a measure of real-world accuracy.
