"""Domain types shared by every layer; status, field and issue-code strings live here only."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

REQUIRED_FIELDS: tuple[str, ...] = (
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "currency",
    "subtotal",
    "tax_amount",
    "total_amount",
)
OPTIONAL_FIELDS: tuple[str, ...] = ("vendor_tax_id", "po_reference")
ALL_FIELDS: tuple[str, ...] = REQUIRED_FIELDS + OPTIONAL_FIELDS
MONEY_FIELDS: tuple[str, ...] = ("subtotal", "tax_amount", "total_amount")

MAX_BPS = 10000
LOW_FIELD_CONFIDENCE_BPS = 8000
LOW_OVERALL_CONFIDENCE_BPS = 8500


class Currency(StrEnum):
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    SGD = "SGD"
    VND = "VND"


class IssueCode(StrEnum):
    MISSING_REQUIRED_FIELD = "missing_required_field"
    MALFORMED_VALUE = "malformed_value"
    UNSUPPORTED_CURRENCY = "unsupported_currency"
    FUTURE_INVOICE_DATE = "future_invoice_date"
    INVALID_AMOUNT_SIGN = "invalid_amount_sign"
    ARITHMETIC_MISMATCH = "arithmetic_mismatch"
    CONFLICTING_VALUES = "conflicting_values"
    POSSIBLE_LOGICAL_DUPLICATE = "possible_logical_duplicate"
    LOW_FIELD_CONFIDENCE = "low_field_confidence"
    LOW_OVERALL_CONFIDENCE = "low_overall_confidence"
    MISSING_PO_REFERENCE = "missing_po_reference"


class Severity(StrEnum):
    BLOCKING = "blocking"
    REVIEW_WARNING = "review_warning"
    ADVISORY = "advisory"


class CandidateStatus(StrEnum):
    AUTO_READY = "auto_ready"
    REVIEW_REQUIRED = "review_required"
    APPROVED = "approved"
    REJECTED = "rejected"


class AttemptStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    ERROR = "error"


class ReviewAction(StrEnum):
    CORRECT = "correct"
    CONFIRM = "confirm"
    CONFIRM_DISTINCT = "confirm_distinct"
    APPROVE = "approve"
    REJECT = "reject"


class ReviewEventType(StrEnum):
    CORRECTED = "corrected"
    CONFIRMED = "confirmed"
    DUPLICATE_CONFIRMED_DISTINCT = "duplicate_confirmed_distinct"
    APPROVAL_BLOCKED = "approval_blocked"
    APPROVED = "approved"
    REJECTED = "rejected"


class RawInvoiceFields(BaseModel):
    """Values exactly as extracted (outer whitespace trimmed). Malformed values are preserved."""

    model_config = ConfigDict(frozen=True)

    vendor_name: str | None = None
    vendor_tax_id: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    currency: str | None = None
    subtotal: str | None = None
    tax_amount: str | None = None
    total_amount: str | None = None
    po_reference: str | None = None

    def to_json_dict(self) -> dict[str, str | None]:
        return self.model_dump()


def money_to_str(value: Decimal) -> str:
    return format(value, "f")


class NormalizedInvoiceFields(BaseModel):
    """Exact-typed values. A field that is missing or failed normalization is ``None``."""

    model_config = ConfigDict(frozen=True)

    vendor_name: str | None = None
    vendor_tax_id: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    currency: Currency | None = None
    subtotal: Decimal | None = None
    tax_amount: Decimal | None = None
    total_amount: Decimal | None = None
    po_reference: str | None = None

    def to_json_dict(self) -> dict[str, str | None]:
        """Canonical strings: ISO dates, plain decimal strings, enum values."""
        out: dict[str, str | None] = {}
        for name in ALL_FIELDS:
            value = getattr(self, name)
            if value is None:
                out[name] = None
            elif isinstance(value, Decimal):
                out[name] = money_to_str(value)
            elif isinstance(value, date):
                out[name] = value.isoformat()
            elif isinstance(value, Currency):
                out[name] = value.value
            else:
                out[name] = value
        return out

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> "NormalizedInvoiceFields":
        data: dict[str, Any] = {}
        for name in ALL_FIELDS:
            value = payload.get(name)
            if value is None:
                data[name] = None
            elif name in MONEY_FIELDS:
                data[name] = Decimal(value)
            elif name == "invoice_date":
                data[name] = date.fromisoformat(value)
            else:
                data[name] = value
        return cls(**data)


class NormalizationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    fields: NormalizedInvoiceFields
    field_errors: dict[str, IssueCode] = Field(default_factory=dict)


class FieldEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    field_name: str
    raw_value: str | None
    confidence_bps: int = Field(ge=0, le=MAX_BPS)


class FieldConflict(BaseModel):
    """Distinct raw values seen for one field. They live here only, never in raw fields."""

    model_config = ConfigDict(frozen=True)

    field_name: str
    values: list[str]


class ValidationIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: IssueCode
    field_name: str | None
    severity: Severity
    resolution: str
    message: str
    candidate_ids: list[str] = Field(default_factory=list)


class ValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    issues: list[ValidationIssue]
    effective_field_confidence: dict[str, int]
    effective_overall_confidence: int = Field(ge=0, le=MAX_BPS)

    @property
    def blocking_issues(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.BLOCKING]

    @property
    def review_warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.REVIEW_WARNING]
