"""The universal explanation floor: caret spans for assertions the rewriter never touched.

pytest rewrites test files only. An `assert` inside a helper module — `tests/helpers.py`, a
shared assertion function, anything imported from application code — raises a bare
`AssertionError` with no explanation at all. That is pytest's biggest fidelity gap, and PEP 657
closes most of it for free: since 3.11 every code object carries column spans for its
instructions, so the failing expression can be underlined without re-evaluating anything.

    tests/helpers.py:14: AssertionError
        assert resp.status_code == expected
               ~~~~~~~~~~~~~~~~~^^~~~~~~~~~

Nothing here re-executes user code, so it is sound in the presence of side effects — the
distinction that killed pytest's `--assert=reinterp` (spec/07 §7). It is strictly weaker than
rewriting (no operand values, just the span), which is exactly why it is the floor and not the
plan: it also makes `--assert=plain` a usable mode rather than a punishment.
"""

from __future__ import annotations

import linecache
from dataclasses import dataclass
from types import TracebackType

__all__ = ["AssertionSource", "explain_assertion", "source_at"]

#: Marks the operator span. Matches CPython's own traceback rendering so the two read alike.
_PRIMARY = "^"
#: Marks the operands either side of it.
_SECONDARY = "~"


@dataclass(frozen=True, slots=True)
class AssertionSource:
    """A failing assertion's source line, plus the caret span under the failing expression."""

    filename: str
    lineno: int
    line: str
    #: `None` when spans are unavailable — a multi-line expression, or code compiled without
    #: position info. Callers show the bare line in that case rather than a misleading caret.
    carets: str | None

    def render(self, indent: str = "    ") -> str:
        """The block shown under a traceback entry."""
        body = [indent + self.line]
        if self.carets is not None:
            body.append(indent + self.carets)
        return "\n".join(body)


def source_at(tb: TracebackType) -> AssertionSource | None:
    """Build the caret block for the instruction `tb` is stopped at.

    Returns None when the source line cannot be read at all — a REPL, an `exec`'d string, a
    file edited since import. A missing source line is normal, not an error.
    """
    frame = tb.tb_frame
    code = frame.f_code
    filename = code.co_filename
    lineno = tb.tb_lineno

    line = linecache.getline(filename, lineno)
    if not line.strip():
        return None
    stripped = line.rstrip("\n")

    return AssertionSource(
        filename=filename,
        lineno=lineno,
        line=stripped.strip(),
        carets=_caret_span(code, tb.tb_lasti, stripped, lineno),
    )


def _caret_span(code: object, lasti: int, line: str, lineno: int) -> str | None:
    """The `~~~^^^~~~` marker for the instruction at `lasti`, aligned to `line.strip()`.

    Two spans are involved: the whole assert-test expression, and — when the test is a
    comparison — the operator inside it. CPython reports the former as the instruction's
    position and the latter is inferred from what remains, which is why the primary carets sit
    under the operator and the secondary tildes under the operands.
    """
    positions = _positions_at(code, lasti)
    if positions is None:
        return None
    start_line, end_line, start_col, end_col = positions

    # A span crossing lines cannot be underlined on a single line. Showing the first line's
    # worth of carets would point at the wrong thing, so show nothing.
    if start_line != lineno or end_line != lineno:
        return None
    if start_col is None or end_col is None or end_col <= start_col:
        return None

    # `line` is the raw source; callers display it stripped, so shift the span to match.
    lead = len(line) - len(line.lstrip())
    start = start_col - lead
    end = end_col - lead
    if start < 0 or end > len(line.strip()):
        return None

    marks = [_SECONDARY] * (end - start)
    operator = _operator_span(line.strip()[start:end])
    if operator is not None:
        for i in range(*operator):
            marks[i] = _PRIMARY
    else:
        # No operator to single out (`assert value`): underline the whole expression evenly.
        marks = [_PRIMARY] * (end - start)

    return " " * start + "".join(marks)


def _positions_at(code: object, lasti: int) -> tuple[int, int, int, int] | None:
    """`(start_line, end_line, start_col, end_col)` for the instruction at `lasti`.

    `co_positions()` yields one entry per instruction word; `lasti` is a byte offset, hence the
    division. Entries may be all-None for synthesised code, which is a normal "no info" answer.
    """
    co_positions = getattr(code, "co_positions", None)
    if co_positions is None:  # pragma: no cover - 3.11+ is the floor for velox
        return None
    try:
        entries = list(co_positions())
    except Exception:  # pragma: no cover - defensive; introspection must never mask the failure
        return None

    index = lasti // 2
    if not 0 <= index < len(entries):
        return None
    start_line, end_line, start_col, end_col = entries[index]
    if start_line is None or end_line is None:
        return None
    return start_line, end_line, start_col, end_col


#: Comparison and membership operators, longest first so `<=` wins over `<` and `not in`
#: over `in`. Identity/membership words are matched on token boundaries below.
_OPERATORS = ("not in", "is not", "==", "!=", ">=", "<=", "in", "is", ">", "<")


def _operator_span(expr: str) -> tuple[int, int] | None:
    """Locate the top-level comparison operator in `expr`, as `(start, end)` offsets.

    Depth-tracked so an operator inside a call or subscript is not mistaken for the one being
    compared. Returns None when there is no comparison — a bare truthiness assert.
    """
    depth = 0
    i = 0
    n = len(expr)
    while i < n:
        char = expr[i]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char in "\"'":
            i = _skip_string(expr, i)
            continue
        elif depth == 0:
            for op in _OPERATORS:
                if not expr.startswith(op, i):
                    continue
                # Word operators need boundaries; symbol operators do not.
                if op[0].isalpha() and not _is_word_boundary(expr, i, len(op)):
                    continue
                return i, i + len(op)
        i += 1
    return None


def _skip_string(expr: str, i: int) -> int:
    """Index just past the string literal starting at `i`, or past `i` if it is unterminated."""
    quote = expr[i]
    j = i + 1
    while j < len(expr):
        if expr[j] == "\\":
            j += 2
            continue
        if expr[j] == quote:
            return j + 1
        j += 1
    return len(expr)


def _is_word_boundary(expr: str, start: int, length: int) -> bool:
    before = expr[start - 1] if start > 0 else " "
    after = expr[start + length] if start + length < len(expr) else " "
    return not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


def explain_assertion(exc: AssertionError) -> str | None:
    """The caret block for an un-rewritten `AssertionError`, or None if unavailable.

    Walks to the *innermost* frame, which is where the `assert` actually is. Returns None when
    the exception already carries a message: a rewritten assert, or an explicit
    `assert x, "why"`, has said something more useful than a caret can.
    """
    if exc.args:
        return None
    tb = exc.__traceback__
    if tb is None:
        return None
    while tb.tb_next is not None:
        tb = tb.tb_next

    source = source_at(tb)
    if source is None or not source.line.startswith("assert"):
        return None
    return source.render()
