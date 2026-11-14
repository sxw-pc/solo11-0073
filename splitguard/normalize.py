"""Text normalization used by exact and near duplicate detection."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping

FIELD_SEPARATOR = "\x1f"
_WHITESPACE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    """NFKC, case-fold, collapse whitespace and strip."""
    folded = unicodedata.normalize("NFKC", value).casefold()
    return _WHITESPACE.sub(" ", folded).strip()


def combine_text_fields(
    values: Mapping[str, str], columns: Iterable[str]
) -> str:
    return FIELD_SEPARATOR.join(normalize_text(values.get(column, "")) for column in columns)


def is_empty_content(normalized: str) -> bool:
    if not normalized:
        return True
    return all(part == "" for part in normalized.split(FIELD_SEPARATOR))


def redacted_summary(normalized: str) -> dict[str, object]:
    """A safe, non-reversible summary; raw normalized text is never emitted."""
    return {
        "masked": "***",
        "normalized_char_count": len(normalized),
        "normalized_token_count": len(normalized.split()),
    }
