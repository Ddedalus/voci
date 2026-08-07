# ruff: noqa
# fmt: off
# Vendored from pytest — DO NOT EDIT BY HAND.
# Source: _pytest/assertion/highlight.py
# pytest commit: 9.2.0.dev0-165-g28e86a6c2
# Regenerate with: uv run python scripts/vendor_assertion.py
# Kept byte-identical to upstream apart from the edits listed in ../VENDOR.md (spec/07 Q17).
from __future__ import annotations

from typing import Literal


def dummy_highlighter(source: str, lexer: Literal["diff", "python"] = "python") -> str:
    """Dummy highlighter that returns the text unprocessed.

    Needed for _notin_text, as the diff gets post-processed to only show the "+" part.
    """
    return source
