"""Label-value extraction for the documented synthetic format. No guessing, no fuzzy matching."""

import re
from types import MappingProxyType

from invoice_qa.domain.models import ALL_FIELDS, FieldConflict, RawInvoiceFields
from invoice_qa.extraction.base import ExtractionResult, read_pdf_text

CANONICAL_LABELS = MappingProxyType(
    {
        "vendor_name": "Vendor Name",
        "vendor_tax_id": "Vendor Tax ID",
        "invoice_number": "Invoice Number",
        "invoice_date": "Invoice Date",
        "currency": "Currency",
        "subtotal": "Subtotal",
        "tax_amount": "Tax Amount",
        "total_amount": "Total Amount",
        "po_reference": "PO Reference",
    }
)

ALIASES = MappingProxyType(
    {
        "vendor_name": ("Supplier Name", "Vendor"),
        "vendor_tax_id": ("Tax ID", "Supplier Tax ID"),
        "invoice_number": ("Invoice No", "Invoice #"),
        "invoice_date": ("Date of Invoice", "Issue Date"),
        "currency": ("Currency Code",),
        "subtotal": ("Sub Total", "Net Amount"),
        "tax_amount": ("Tax", "VAT Amount"),
        "total_amount": ("Total", "Amount Due"),
        "po_reference": ("PO Number", "Purchase Order"),
    }
)

# Fields for which a case/punctuation-insensitive match of the canonical label is accepted.
LOOSE_ALIAS_FIELDS = frozenset({"vendor_name"})

EXACT_BPS = 10000
ALIAS_BPS = 9000
LOOSE_BPS = 7500


def _loose_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]", "", label.lower())


_EXACT_LOOKUP = {label: (name, EXACT_BPS) for name, label in CANONICAL_LABELS.items()}
_ALIAS_LOOKUP = {alias: (name, ALIAS_BPS) for name, aliases in ALIASES.items() for alias in aliases}
_LOOSE_LOOKUP = {
    _loose_key(CANONICAL_LABELS[name]): (name, LOOSE_BPS) for name in LOOSE_ALIAS_FIELDS
}


def _match_label(label: str) -> tuple[str, int] | None:
    return (
        _EXACT_LOOKUP.get(label) or _ALIAS_LOOKUP.get(label) or _LOOSE_LOOKUP.get(_loose_key(label))
    )


class DeterministicExtractor:
    method = "deterministic"

    def extract(self, pdf_bytes: bytes) -> ExtractionResult:
        try:
            text = read_pdf_text(pdf_bytes)
        except Exception:
            return ExtractionResult(method=self.method, error_code="extraction_failed")
        if not text.strip():
            return ExtractionResult(method=self.method, error_code="no_extractable_text")

        found: dict[str, list[tuple[str, int]]] = {}
        for line in text.splitlines():
            label, separator, value = line.partition(":")
            value = value.strip()
            if not separator or not value:
                continue
            match = _match_label(label.strip())
            if match is not None:
                found.setdefault(match[0], []).append((value, match[1]))

        values: dict[str, str | None] = {}
        confidence: dict[str, int] = {}
        conflicts: list[FieldConflict] = []
        for name in ALL_FIELDS:
            hits = found.get(name, [])
            distinct = list(dict.fromkeys(value for value, _ in hits))
            if not hits:
                values[name], confidence[name] = None, 0
            elif len(distinct) > 1:
                values[name], confidence[name] = None, 0
                conflicts.append(FieldConflict(field_name=name, values=distinct))
            else:
                values[name] = distinct[0]
                confidence[name] = max(bps for _, bps in hits)
        return ExtractionResult(
            method=self.method,
            raw=RawInvoiceFields(**values),
            field_confidence_bps=confidence,
            conflicts=conflicts,
        )
