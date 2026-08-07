# ruff: noqa
# fmt: off
# Vendored from pytest — DO NOT EDIT BY HAND.
# Source: _pytest/assertion/_compare_any.py
# pytest commit: 9.2.0.dev0-165-g28e86a6c2
# Regenerate with: uv run python scripts/vendor_assertion.py
# Kept byte-identical to upstream apart from the edits listed in ../VENDOR.md (spec/07 Q17).
from __future__ import annotations

from collections.abc import Iterator
import dataclasses
import pprint

from velox._vendor.assertion._compare_mapping import _compare_eq_mapping
from velox._vendor.assertion._compare_sequence import _compare_eq_iterable
from velox._vendor.assertion._compare_sequence import _compare_eq_sequence
from velox._vendor.assertion._compare_set import _compare_eq_set
from velox._vendor.assertion._guards import has_default_eq
from velox._vendor.assertion._guards import isattrs
from velox._vendor.assertion._guards import isdatacls
from velox._vendor.assertion._guards import isiterable
from velox._vendor.assertion._guards import ismapping
from velox._vendor.assertion._guards import isnamedtuple
from velox._vendor.assertion._guards import issequence
from velox._vendor.assertion._guards import isset
from velox._vendor.assertion._guards import istext
from velox._vendor.assertion._typing import _AssertionTextDiffStyle
from velox._vendor.assertion._typing import _HighlightFunc
from velox._vendor.assertion._typing import NO_TRUNCATION_BUDGET
from velox._vendor.assertion._typing import TruncationBudget
from velox._vendor.assertion.compare_text import _compare_eq_text


def _velox_approx_compare(approx: object, other: object) -> Iterator[str]:
    """Detailed lines for an approx comparison, if this Approx can produce any."""
    repr_compare = getattr(approx, "_repr_compare", None)
    if repr_compare is None:
        return iter(())
    return repr_compare(other)


def _compare_eq_any(
    left: object,
    right: object,
    highlighter: _HighlightFunc,
    verbose: int,
    assertion_text_diff_style: _AssertionTextDiffStyle,
    truncation_budget: TruncationBudget = NO_TRUNCATION_BUDGET,
) -> Iterator[str]:
    """Yield the per-line explanation for ``left == right`` (without summary).

    Yields nothing when no specialised explanation applies, so consumers
    can stream the output and bail out early (e.g. for truncation) without
    materialising the entire diff first.
    """
    if istext(left) and istext(right):
        yield from _compare_eq_text(
            left,
            right,
            highlighter,
            verbose,
            assertion_text_diff_style,
            truncation_budget,
        )
    else:
        from velox._assertions import Approx

        # Although the common order should be obtained == approx(...), allow both ways.
        # velox: _repr_compare is optional; a scalar approx has no diff worth showing.
        if isinstance(right, Approx):
            yield from _velox_approx_compare(right, left)
        elif isinstance(left, Approx):
            yield from _velox_approx_compare(left, right)
        elif type(left) is type(right) and (
            isdatacls(left) or isattrs(left) or isnamedtuple(left)
        ):
            # Note: unlike dataclasses/attrs, namedtuples compare only the
            # field values, not the type or field names. But this branch
            # intentionally only handles the same-type case, which was often
            # used in older code bases before dataclasses/attrs were available.
            yield from _compare_eq_cls(
                left,
                right,
                highlighter,
                verbose,
                assertion_text_diff_style,
            )
        elif issequence(left) and issequence(right):
            yield from _compare_eq_sequence(left, right, highlighter, verbose)
        elif isset(left) and isset(right):
            yield from _compare_eq_set(left, right, highlighter, verbose)
        elif ismapping(left) and ismapping(right):
            yield from _compare_eq_mapping(
                left, right, highlighter, verbose, truncation_budget
            )

        if isiterable(left) and isiterable(right):
            yield from _compare_eq_iterable(
                left, right, highlighter, verbose, truncation_budget
            )


def _compare_eq_cls(
    left: object,
    right: object,
    highlighter: _HighlightFunc,
    verbose: int,
    assertion_text_diff_style: _AssertionTextDiffStyle,
) -> Iterator[str]:
    if not has_default_eq(left):
        return
    if isdatacls(left):
        all_fields = dataclasses.fields(left)
        fields_to_check = [info.name for info in all_fields if info.compare]
    elif isattrs(left):
        all_fields = left.__attrs_attrs__  # type: ignore[attr-defined]
        fields_to_check = [field.name for field in all_fields if getattr(field, "eq")]
    elif isnamedtuple(left):
        fields_to_check = left._fields  # type: ignore[attr-defined]
    else:
        assert False

    indent = "  "
    same = []
    diff = []
    for field in fields_to_check:
        if getattr(left, field) == getattr(right, field):
            same.append(field)
        else:
            diff.append(field)

    if same or diff:
        yield ""
    if same and verbose < 2:
        yield f"Omitting {len(same)} identical items, use -vv to show"
    elif same:
        yield "Matching attributes:"
        yield from highlighter(pprint.pformat(same)).splitlines()
    if diff:
        yield "Differing attributes:"
        yield from highlighter(pprint.pformat(diff)).splitlines()
        for field in diff:
            field_left = getattr(left, field)
            field_right = getattr(right, field)
            yield ""
            yield f"Drill down into differing attribute {field}:"
            yield f"{indent}{field}: {highlighter(repr(field_left))} != {highlighter(repr(field_right))}"
            for line in _compare_eq_any(
                field_left,
                field_right,
                highlighter,
                verbose,
                assertion_text_diff_style,
            ):
                yield indent + line
