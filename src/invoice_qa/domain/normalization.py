"""Exact normalization. Used for extraction and for reviewer corrections alike."""

import re
from datetime import date
from decimal import Context, Decimal, Inexact, InvalidOperation

from invoice_qa.domain.models import (
    MONEY_FIELDS,
    Currency,
    IssueCode,
    NormalizationResult,
    NormalizedInvoiceFields,
    RawInvoiceFields,
)

AMOUNT_PATTERN = re.compile(r"[+-]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?", re.ASCII)
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
TAX_ID_PATTERN = re.compile(r"DEMO-TAX-\d{4}", re.ASCII)

FRACTION_DIGITS: dict[Currency, int] = {
    Currency.VND: 0,
    Currency.USD: 2,
    Currency.EUR: 2,
    Currency.GBP: 2,
    Currency.SGD: 2,
}

# Quantize may never round: any lost digit raises instead of changing a value.
_EXACT = Context(prec=60, traps=[Inexact, InvalidOperation])


def collapse_whitespace(value: str) -> str:
    return " ".join(value.split())


def _present(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


def _parse_amount(text: str, currency: Currency | None) -> Decimal | None:
    if not AMOUNT_PATTERN.fullmatch(text):
        return None
    fraction = text.partition(".")[2]
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    if currency is None:
        return value
    digits = FRACTION_DIGITS[currency]
    if len(fraction) > digits:
        return None
    try:
        return value.quantize(Decimal(1).scaleb(-digits), context=_EXACT)
    except (InvalidOperation, Inexact):
        return None


def _parse_date(text: str) -> date | None:
    if not DATE_PATTERN.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def normalize_invoice(raw: RawInvoiceFields) -> NormalizationResult:
    values: dict[str, object] = {}
    errors: dict[str, IssueCode] = {}

    currency: Currency | None = None
    currency_text = _present(raw.currency)
    if currency_text is not None:
        try:
            currency = Currency(currency_text)
        except ValueError:
            errors["currency"] = IssueCode.UNSUPPORTED_CURRENCY
    values["currency"] = currency

    name = _present(raw.vendor_name)
    values["vendor_name"] = collapse_whitespace(name) if name else None
    values["invoice_number"] = _present(raw.invoice_number)
    values["po_reference"] = _present(raw.po_reference)

    date_text = _present(raw.invoice_date)
    parsed_date = _parse_date(date_text) if date_text else None
    if date_text and parsed_date is None:
        errors["invoice_date"] = IssueCode.MALFORMED_VALUE
    values["invoice_date"] = parsed_date

    tax_text = _present(raw.vendor_tax_id)
    tax_id = tax_text.upper() if tax_text else None
    if tax_id is not None and not TAX_ID_PATTERN.fullmatch(tax_id):
        errors["vendor_tax_id"] = IssueCode.MALFORMED_VALUE
        tax_id = None
    values["vendor_tax_id"] = tax_id

    for field_name in MONEY_FIELDS:
        text = _present(getattr(raw, field_name))
        amount = _parse_amount(text, currency) if text else None
        if text and amount is None:
            errors[field_name] = IssueCode.MALFORMED_VALUE
        values[field_name] = amount

    return NormalizationResult(fields=NormalizedInvoiceFields(**values), field_errors=errors)
