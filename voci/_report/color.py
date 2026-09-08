"""ANSI color codes for the terminal reporter and `cli.main`'s own prints, plus the one
place that decides whether they're used at all.

Both `Reporter` and `cli.main` print lines that belong to the same report (file blocks,
`SKIPPED`/`COLLECTION ERROR` lines, the summary), so the enabled/disabled decision lives
here once and both call `paint` against it, rather than each guessing independently and
risking a report that's colored in one place and plain in another.
"""

from __future__ import annotations

import os
from typing import TextIO

__all__ = ["GRAY", "GREEN", "PRIMARY", "RED", "YELLOW", "color_enabled", "counts", "paint"]

_RESET = "\x1b[0m"

#: Outcome colors: pass, skip, fail/error/timeout -- matched to what voci already
#: calls those outcomes elsewhere (`PASS`/`FAIL` blocks, `SKIPPED`, `TIMEOUT`/`ERROR`).
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
#: Supporting text (labels, punctuation, timings) -- bright black, not `\x1b[2m` dim:
#: some terminals render dim as genuinely low-contrast or ignore it outright, where
#: bright-black reliably reads as "quieter than the surrounding text" everywhere.
GRAY = "\x1b[90m"
#: Primary datapoints (ids, paths, the leading test count) -- bold in the terminal's
#: own foreground color, not a literal white/black: a hardcoded white is invisible on a
#: light-background terminal, and bold is the portable way to say "this is the point"
#: without assuming which theme the terminal is in.
PRIMARY = "\x1b[1m"


def color_enabled(stream: TextIO) -> bool:
    """True iff `stream` is a real terminal and the user hasn't opted out via `NO_COLOR`
    (https://no-color.org -- any value, including empty, disables color per that spec)."""
    if "NO_COLOR" in os.environ:
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def paint(text: str, code: str, *, enabled: bool) -> str:
    """Wraps `text` in `code`/reset, or returns it unchanged when `enabled` is False --
    every call site takes `enabled` explicitly rather than re-reading global state, so a
    non-tty stream (piped output, a captured test) never gets stray escape codes."""
    if not enabled:
        return text
    return f"{code}{text}{_RESET}"


def counts(*fields: tuple[int, str, str], enabled: bool) -> str:
    """`(count, label, color)` triples joined into `2 skipped · 1 deselected`, dropping every
    zero count. Empty when they were all zero."""
    return " · ".join(
        paint(f"{count} {label}", color, enabled=enabled) for count, label, color in fields if count
    )
