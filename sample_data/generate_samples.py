"""Generate the synthetic evaluation PDFs into a gitignored directory.

Usage: python sample_data/generate_samples.py [--manifest PATH] [--output DIR]
"""

import argparse
import json
from pathlib import Path

from invoice_qa.infrastructure.file_security import sha256_bytes
from invoice_qa.synthetic import build_no_text_pdf, fixture_pdf

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "sample_data" / "manifest.json"
DEFAULT_OUTPUT = ROOT / "sample_data" / "generated"
# Not an evaluation fixture: a PDF without a text layer for the manual retry walkthrough.
RETRY_DEMO_ID = "retry-demo-no-text"


def generate(manifest_path: Path, output_dir: Path) -> dict[str, str]:
    """Write one PDF per fixture plus the retry-demo PDF; return {file id: SHA-256}."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    digests = {}
    for fixture in manifest["fixtures"]:
        content = fixture_pdf(fixture)
        (output_dir / f"{fixture['id']}.pdf").write_bytes(content)
        digests[fixture["id"]] = sha256_bytes(content)
    retry_demo = build_no_text_pdf()
    (output_dir / f"{RETRY_DEMO_ID}.pdf").write_bytes(retry_demo)
    digests[RETRY_DEMO_ID] = sha256_bytes(retry_demo)
    return digests


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    digests = generate(args.manifest, args.output)
    for fixture_id, digest in digests.items():
        print(f"{fixture_id}  {digest[:16]}")
    print(f"Generated {len(digests)} synthetic PDFs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
