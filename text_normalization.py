"""Small text-normalization helpers shared by transcription and minutes output."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional


@lru_cache(maxsize=1)
def _opencc_converter() -> Optional[Any]:
    """Load OpenCC once; keep the app usable if an optional install is missing."""
    try:
        from opencc import OpenCC
    except ImportError:
        return None
    try:
        return OpenCC("t2s")
    except Exception:
        return None


def to_simplified_chinese(value: Any) -> str:
    """Convert Traditional Chinese to Simplified Chinese without changing other text."""
    text = str(value or "")
    converter = _opencc_converter()
    if converter is None:
        return text
    try:
        return converter.convert(text)
    except Exception:
        return text
