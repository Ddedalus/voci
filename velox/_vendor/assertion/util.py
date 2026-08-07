# ruff: noqa
# fmt: off
# Vendored from pytest — DO NOT EDIT BY HAND.
# Source: _pytest/assertion/util.py
# pytest commit: 9.2.0.dev0-165-g28e86a6c2
# Regenerate with: uv run python scripts/vendor_assertion.py
# Kept byte-identical to upstream apart from the edits listed in ../VENDOR.md (spec/07 Q17).
# mypy: allow-untyped-defs
"""Utilities for assertion debugging."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Sequence
from unicodedata import normalize

from velox._vendor.assertion._shim import outcomes
from velox._vendor.assertion._shim import repr_crash as _shim_repr_crash
from velox._vendor.assertion.saferepr import saferepr
from velox._vendor.assertion.saferepr import saferepr_unlimited
from velox._vendor.assertion._compare_any import _compare_eq_any
from velox._vendor.assertion._compare_set import SET_COMPARISON_FUNCTIONS
from velox._vendor.assertion._guards import isset
from velox._vendor.assertion._guards import istext
from velox._vendor.assertion._typing import _AssertionTextDiffStyle
from velox._vendor.assertion._typing import _HighlightFunc
from velox._vendor.assertion._typing import NO_TRUNCATION_BUDGET
from velox._vendor.assertion._typing import TruncationBudget
from velox._vendor.assertion.compare_text import _notin_text
from velox._vendor.assertion.highlight import dummy_highlighter as dummy_highlighter
from velox._vendor.assertion._shim import Config


# velox: upstream these were plain module globals that pytest save/restored around each test
# item — the one genuine concurrency blocker in this subsystem, since velox runs tests as
# concurrent asyncio tasks sharing a process (spec/07 §4.1). They are ContextVars now, read
# through a PEP 562 module __getattr__ so the vendored rewriter's `util._reprcompare` lookups
# are untouched. Do NOT assign to these names: a real global would shadow __getattr__ and
# silently restore the old, unsafe behaviour. Use velox._assertion_state instead.
from velox._assertion_state import CONTEXT_GLOBALS as _velox_context_globals


def __getattr__(name: str) -> object:
    try:
        var = _velox_context_globals[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    return var.get()


def format_explanation(explanation: str) -> str:
    r"""Format an explanation.

    Normally all embedded newlines are escaped, however there are
    three exceptions: \n{, \n} and \n~.  The first two are intended
    cover nested explanations, see function and attribute explanations
    for examples (.visit_Call(), visit_Attribute()).  The last one is
    for when one explanation needs to span multiple lines, e.g. when
    displaying diffs.
    """
    lines = _split_explanation(explanation)
    result = _format_lines(lines)
    return "\n".join(result)


def _split_explanation(explanation: str) -> list[str]:
    r"""Return a list of individual lines in the explanation.

    This will return a list of lines split on '\n{', '\n}' and '\n~'.
    Any other newlines will be escaped and appear in the line as the
    literal '\n' characters.
    """
    raw_lines = (explanation or "").split("\n")
    lines = [raw_lines[0]]
    for values in raw_lines[1:]:
        if values and values[0] in ["{", "}", "~", ">"]:
            lines.append(values)
        else:
            lines[-1] += "\\n" + values
    return lines


def _format_lines(lines: Sequence[str]) -> list[str]:
    """Format the individual lines.

    This will replace the '{', '}' and '~' characters of our mini formatting
    language with the proper 'where ...', 'and ...' and ' + ...' text, taking
    care of indentation along the way.

    Return a list of formatted lines.
    """
    result = list(lines[:1])
    stack = [0]
    stackcnt = [0]
    for line in lines[1:]:
        if line.startswith("{"):
            if stackcnt[-1]:
                s = "and   "
            else:
                s = "where "
            stack.append(len(result))
            stackcnt[-1] += 1
            stackcnt.append(0)
            result.append(" +" + "  " * (len(stack) - 1) + s + line[1:])
        elif line.startswith("}"):
            stack.pop()
            stackcnt.pop()
            result[stack[-1]] += line[1:]
        else:
            assert line[0] in ["~", ">"]
            stack[-1] += 1
            indent = len(stack) if line.startswith("~") else len(stack) - 1
            result.append("  " * indent + line[1:])
    assert len(stack) == 1
    return result


def assertrepr_compare(
    op: str,
    left: object,
    right: object,
    *,
    verbose: int,
    highlighter: _HighlightFunc,
    assertion_text_diff_style: _AssertionTextDiffStyle,
    truncation_budget: TruncationBudget = NO_TRUNCATION_BUDGET,
) -> Iterator[str]:
    """Yield specialised explanations for some operators/operands.

    The first line yielded is always the summary (``left op right``);
    subsequent lines are the detailed explanation. Yields nothing when no
    specialised explanation applies, which lets consumers map an empty
    iterator to "no explanation" without materialising anything.

    The iterator is lazy on purpose: a streaming consumer can stop pulling
    lines as soon as it has enough to show, so an enormous diff doesn't
    have to be built in full just to be thrown away.
    """
    # Strings which normalize equal are often hard to distinguish when printed; use ascii() to make this easier.
    # See issue #3246.
    use_ascii = (
        isinstance(left, str)
        and isinstance(right, str)
        and normalize("NFD", left) == normalize("NFD", right)
    )

    if verbose > 1:
        left_repr = saferepr_unlimited(left, use_ascii=use_ascii)
        right_repr = saferepr_unlimited(right, use_ascii=use_ascii)
    else:
        # XXX: "15 chars indentation" is wrong
        #      ("E       AssertionError: assert "); should use term width.
        maxsize = (
            80 - 15 - len(op) - 2
        ) // 2  # 15 chars indentation, 1 space around op

        left_repr = saferepr(left, maxsize=maxsize, use_ascii=use_ascii)
        right_repr = saferepr(right, maxsize=maxsize, use_ascii=use_ascii)

    summary = f"{left_repr} {op} {right_repr}"

    try:
        if op == "==":
            source = _compare_eq_any(
                left,
                right,
                highlighter,
                verbose,
                assertion_text_diff_style,
                truncation_budget,
            )
        elif op == "not in" and istext(left) and istext(right):
            source = _notin_text(left, right, verbose, truncation_budget)
        elif op in {"!=", ">=", "<=", ">", "<"} and isset(left) and isset(right):
            source = SET_COMPARISON_FUNCTIONS[op](left, right, highlighter, verbose)
        else:
            source = iter(())

        # Only yield the summary if there is a detailed explanation.
        # Make sure there's a separating empty line after the summary.
        summary_yielded = False
        for line in source:
            if not summary_yielded:
                yield summary
                if line != "":
                    yield ""
                summary_yielded = True
            yield line
    except outcomes.Exit:
        raise
    except Exception:
        repr_crash = _shim_repr_crash()
        if not summary_yielded:
            yield summary
            yield ""
            summary_yielded = True
        yield (
            f"(velox: representation of details failed: {repr_crash}."
        )
        yield " Probably an object has a faulty __repr__.)"
