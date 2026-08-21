"""Shared validation and log-safety helpers for listing detail requests."""

import re
import unicodedata


MAX_BATCH_SIZE = 50
MAX_BATCH_CONCURRENCY = 10
MAX_LISTING_ID_LENGTH = 42
MAX_BATCH_ID_LENGTH = 64

# Kleinanzeigen accepts either the numeric ad ID or the canonical routing
# segment ``ad-id-category-id-location-id`` returned by search results.
LISTING_ID_PATTERN = r"^[0-9]{6,20}(?:-[0-9]{1,10}-[0-9]{1,10})?$"
BATCH_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"

_LISTING_ID_RE = re.compile(LISTING_ID_PATTERN)


def normalize_listing_id(value: str) -> str:
    """Return a canonical supported listing ID or raise ``ValueError``."""
    if not isinstance(value, str):
        raise ValueError("Listing ID must be a string")
    normalized = value.strip()
    if len(normalized) > MAX_LISTING_ID_LENGTH or not _LISTING_ID_RE.fullmatch(
        normalized
    ):
        raise ValueError(
            "Listing ID must be a numeric ad ID or an "
            "ad-id-category-id-location-id segment"
        )
    return normalized


def sanitize_log_value(value: object, *, max_length: int = 128) -> str:
    """Render untrusted data as one bounded, control-character-free log field."""
    text = str(value)
    truncated = len(text) > max_length
    text = text[:max_length]

    safe_parts: list[str] = []
    for character in text:
        category = unicodedata.category(character)
        if category.startswith("C") or category in {"Zl", "Zp"}:
            codepoint = ord(character)
            escape = "\\x" if codepoint <= 0xFF else "\\u"
            width = 2 if codepoint <= 0xFF else 4
            safe_parts.append(f"{escape}{codepoint:0{width}x}")
        else:
            safe_parts.append(character)

    if truncated:
        safe_parts.append("…")
    return "".join(safe_parts)
