"""Upload checks. Order: empty, extension, size, magic bytes, parse, encryption, page count."""

import hashlib
import re
from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
GENERIC_MESSAGE = "The file could not be accepted."


class UploadRejected(Exception):  # noqa: N818 - domain name reads better than *Error
    """Rejection with a stable machine code. The message is deliberately generic."""

    def __init__(self, code: str) -> None:
        super().__init__(GENERIC_MESSAGE)
        self.code = code


@dataclass(frozen=True)
class PdfInspection:
    page_count: int
    size_bytes: int


def validate_pdf_upload(
    filename: str,
    content: bytes,
    *,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> PdfInspection:
    if not content:
        raise UploadRejected("empty_file")
    if not filename.lower().endswith(".pdf"):
        raise UploadRejected("invalid_extension")
    if len(content) > max_bytes:
        raise UploadRejected("file_too_large")
    if not content.startswith(b"%PDF-"):
        raise UploadRejected("invalid_pdf_header")
    try:
        reader = PdfReader(BytesIO(content))
        encrypted = reader.is_encrypted
        page_count = 0 if encrypted else len(reader.pages)
    except Exception as exc:  # any parser failure is a generic structural rejection
        raise UploadRejected("unreadable_pdf") from exc
    if encrypted:
        raise UploadRejected("encrypted_pdf")
    if page_count < 1:
        raise UploadRejected("no_pages")
    return PdfInspection(page_count=page_count, size_bytes=len(content))


def safe_display_filename(filename: str) -> str:
    name = re.split(r"[\\/]", filename)[-1]
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return name[:100].strip(" .") or "upload.pdf"


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
