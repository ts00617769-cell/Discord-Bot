"""東亞字寬對齊輔助。"""
from __future__ import annotations

import unicodedata


def display_width(text) -> int:
    return sum(
        2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text)
    )


def pad_text(text, target_width: int) -> str:
    text_str = str(text)
    return text_str + " " * max(0, target_width - display_width(text_str))
