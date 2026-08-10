"""Comparison-diff explanations, ported from pytest's `testing/test_assertion.py`.

The helpers in `conftest.py` are shaped like upstream's `callequal`/`callop`, so these can be
diffed directly against `testing/test_assertion.py`.
"""

from __future__ import annotations

import pytest
from velox._vendor.assertion import util
from velox._vendor.assertion._compare_any import _compare_eq_cls
from velox._vendor.assertion._typing import NO_TRUNCATION_BUDGET, TruncationBudget
from velox._vendor.assertion.compare_text import _compare_eq_text, _notin_text

from .conftest import callequal, callop


class TestReprCompare:
    """Ported from upstream `TestAssert_reprcompare`."""

    def test_different_types(self) -> None:
        assert callequal([0, 1], "foo") is None

    def test_summary(self) -> None:
        lines = callequal([0, 1], [0, 2])
        assert lines is not None
        summary = lines[0]
        assert len(summary) < 65

    def test_text_diff(self) -> None:
        assert callequal("spam", "eggs") == [
            "'spam' == 'eggs'",
            "",
            "- eggs",
            "+ spam",
        ]

    def test_text_diff_ndiff_style(self) -> None:
        assert list(_compare_eq_text("spam", "eggs", util.dummy_highlighter, 0, "ndiff")) == [
            "- eggs",
            "+ spam",
        ]

    def test_text_skipping(self) -> None:
        lines = callequal("a" * 50 + "spam", "a" * 50 + "eggs")
        assert lines is not None
        assert "Skipping" in lines[2]
        for line in lines:
            assert "a" * 50 not in line

    def test_text_skipping_verbose(self) -> None:
        lines = callequal("a" * 50 + "spam", "a" * 50 + "eggs", verbose=1)
        assert lines is not None
        assert "- " + "a" * 50 + "eggs" in lines
        assert "+ " + "a" * 50 + "spam" in lines

    def test_multiline_text_diff(self) -> None:
        diff = callequal("foo\nspam\nbar", "foo\neggs\nbar")
        assert diff is not None
        assert "- eggs" in diff
        assert "+ spam" in diff

    def test_multiline_text_diff_block(self) -> None:
        assert callequal("foo\nspam\nbar", "foo\neggs\nbar", assertion_text_diff_style="block") == [
            r"'foo\nspam\nbar' == 'foo\neggs\nbar'",
            "",
            "Left:",
            "  foo",
            "  spam",
            "  bar",
            "",
            "Right:",
            "  foo",
            "  eggs",
            "  bar",
        ]

    def test_single_line_text_diff_block(self) -> None:
        assert callequal("spam", "eggs", assertion_text_diff_style="block") == [
            "'spam' == 'eggs'",
            "",
            "Left:",
            "  spam",
            "",
            "Right:",
            "  eggs",
        ]

    def test_bytes_diff_normal(self) -> None:
        """Bytes get index-wise treatment rather than a character diff (upstream #5260)."""
        assert callequal(b"spam", b"eggs") == [
            "b'spam' == b'eggs'",
            "",
            "At index 0 diff: b's' != b'e'",
            "Use -v to get more diff",
        ]

    def test_bytes_diff_verbose(self) -> None:
        assert callequal(b"spam", b"eggs", verbose=1) == [
            "b'spam' == b'eggs'",
            "",
            "At index 0 diff: b's' != b'e'",
            "",
            "Full diff: (-: missing in left side, +: extra in left side)",
            "- b'eggs'",
            "+ b'spam'",
        ]

    def test_list(self) -> None:
        expl = callequal([0, 1], [0, 2])
        assert expl is not None
        assert len(expl) > 1

    def test_list_different_lengths(self) -> None:
        expl = callequal([0, 1], [0, 1, 2])
        assert expl is not None
        assert len(expl) > 1
        expl = callequal([0, 1, 2], [0, 1])
        assert expl is not None
        assert len(expl) > 1

    def test_dict(self) -> None:
        expl = callequal({"a": 0}, {"a": 1})
        assert expl is not None
        assert len(expl) > 1

    def test_dict_omitting(self) -> None:
        lines = callequal({"a": 0, "b": 1}, {"a": 1, "b": 1})
        assert lines is not None
        assert lines[2].startswith("Omitting 1 identical item")
        assert "Common items" not in lines
        for line in lines[2:]:
            assert "b" not in line

    def test_dict_omitting_with_verbosity_1(self) -> None:
        """Ensure differing items are visible when omitting identical ones."""
        lines = callequal({"a": 0, "b": 1}, {"a": 1, "b": 1}, verbose=1)
        assert lines is not None
        assert lines[2].startswith("Omitting 1 identical item")
        assert lines[3].startswith("Differing items")
        assert lines[4] == "{'a': 0} != {'a': 1}"
        assert "Common items" not in lines

    def test_dict_omitting_with_verbosity_2(self) -> None:
        lines = callequal({"a": 0, "b": 1}, {"a": 1, "b": 1}, verbose=2)
        assert lines is not None
        assert lines[2].startswith("Common items:")
        assert "Omitting" not in lines[2]
        assert lines[3] == "{'b': 1}"

    def test_set(self) -> None:
        expl = callequal({0, 1}, {0, 2})
        assert expl is not None
        assert len(expl) > 1

    def test_frozenzet(self) -> None:
        expl = callequal(frozenset([0, 1]), {0, 2})
        assert expl is not None
        assert len(expl) > 1

    def test_Sequence(self) -> None:
        """A `collections.abc.Sequence` that is not a list still gets sequence treatment."""

        class TestSequence(list):
            def __init__(self, iterable):
                list.__init__(self, iterable)

        expl = callequal(TestSequence([0, 1]), list([0, 2]))
        assert expl is not None
        assert len(expl) > 1

    def test_list_tuples(self) -> None:
        expl = callequal([], [(1, 2)])
        assert expl is not None
        assert len(expl) > 1
        expl = callequal([(1, 2)], [])
        assert expl is not None
        assert len(expl) > 1

    def test_list_bad_repr(self) -> None:
        """A `__repr__` that raises must not take the whole explanation down with it."""

        class A:
            def __repr__(self):
                raise ValueError(42)

        expl = callequal([], [A()])
        assert expl is not None
        assert "ValueError" in "".join(expl)

        expl = callequal({}, {"1": A()}, verbose=2)
        assert expl is not None
        assert expl[0].startswith("{} == <[ValueError")
        assert "raised in repr" in expl[0]

    def test_one_repr_empty(self) -> None:
        """A `__repr__` returning the empty string must not crash the diff (upstream #2160)."""

        class A(str):
            def __repr__(self):
                return ""

        expl = callequal(A(), "")
        assert not expl

    def test_repr_no_exc(self) -> None:
        expl = callequal("foo", "bar")
        assert expl is not None
        assert "raised in repr()" not in " ".join(expl)

    def test_unicode(self) -> None:
        assert callequal("£€", "£") == [
            "'£€' == '£'",
            "",
            "- £",
            "+ £€",
        ]

    def test_nonascii_text(self) -> None:
        """Non-ascii bytes with a lossy `__repr__` must still produce a diff (upstream #877)."""

        class A(str):
            def __repr__(self):
                return "\xff"

        expl = callequal(A(), "1")
        assert expl == ["ÿ == '1'", "", "- 1"]

    def test_format_nonascii_explanation(self) -> None:
        assert util.format_explanation("λ")

    def test_mojibake(self) -> None:
        """Bytes that are not valid utf-8 must not raise while being explained."""
        left = b"e"
        right = b"\xc3\xa9"
        expl = callequal(left, right)
        assert expl is not None
        for line in expl:
            assert isinstance(line, str)
        assert "\\xc3\\xa9" in "\n".join(expl)


class TestNotIn:
    """`not in` shares the text-diff machinery with `==`."""

    def test_notin_text(self) -> None:
        lines = callop("not in", "foo", "aaafoobbb")
        assert lines is not None
        assert any("foo" in line for line in lines)

    def test_notin_non_text_is_unexplained(self) -> None:
        assert callop("not in", 1, [1, 2, 3]) is None


class TestSetComparisons:
    """Ordering operators on sets get an explanation; on other types they do not."""

    @pytest.mark.parametrize("op", ["<", "<=", ">", ">="])
    def test_set_ordering_explained(self, op: str) -> None:
        lines = callop(op, {1, 2}, {1, 3})
        assert lines is not None
        assert len(lines) > 1

    @pytest.mark.parametrize("op", ["<", "<=", ">", ">="])
    def test_non_set_ordering_unexplained(self, op: str) -> None:
        assert callop(op, [1, 2], [1, 3]) is None


class TestTruncationBudget:
    """The truncation budget bounds the work done building an explanation, not just the
    output shown."""

    def test_text_diff_budget_caps_ndiff_input(self) -> None:
        left = "\n".join(f"left {i}" for i in range(1000))
        right = "\n".join(f"right {i}" for i in range(1000))
        capped = list(
            _compare_eq_text(
                left,
                right,
                util.dummy_highlighter,
                1,
                "ndiff",
                TruncationBudget(max_lines=11, max_chars=710),
            )
        )
        full = list(
            _compare_eq_text(left, right, util.dummy_highlighter, 1, "ndiff", NO_TRUNCATION_BUDGET)
        )
        assert len(capped) < 80
        assert len(full) > 1500

    def test_budget_caps_each_line(self) -> None:
        capped = list(
            _compare_eq_text(
                "x" * 100_000,
                "y" * 100_000,
                util.dummy_highlighter,
                1,
                "ndiff",
                TruncationBudget(max_lines=11, max_chars=710),
            )
        )
        assert all(len(line) < 1000 for line in capped)

    def test_notin_text_budget_caps_ndiff_input(self) -> None:
        needle = "NEEDLE"
        text = "a" * 100_000 + needle + "a" * 100_000
        capped = list(_notin_text(needle, text, 1, TruncationBudget(max_lines=11, max_chars=710)))
        full = list(_notin_text(needle, text, 1, NO_TRUNCATION_BUDGET))
        assert len(capped) < 80
        assert all(len(line) < 1000 for line in capped)
        assert sum(len(line) for line in full) > 100_000

    def test_explanation_is_lazy(self) -> None:
        """`assertrepr_compare` yields; a consumer that stops early does not pay for the rest."""
        pulled = 0

        def counting_left() -> list[int]:
            return list(range(10_000))

        lines = util.assertrepr_compare(
            op="==",
            left=counting_left(),
            right=[0],
            verbose=2,
            highlighter=util.dummy_highlighter,
            assertion_text_diff_style="ndiff",
        )
        for _ in lines:
            pulled += 1
            if pulled > 3:
                break
        assert pulled == 4


class TestDataclassAndAttrs:
    def test_dataclass_fields(self) -> None:
        from dataclasses import dataclass

        @dataclass
        class Point:
            x: int
            y: int

        expl = callequal(Point(1, 2), Point(1, 3))
        assert expl is not None
        assert any("Differing attributes" in line for line in expl)
        assert any("y" in line for line in expl)

    def test_compare_eq_cls_no_comparable_fields(self) -> None:
        """A dataclass with every field `compare=False` has nothing to diff."""
        from dataclasses import dataclass, field

        @dataclass
        class Foo:
            x: int = field(compare=False)

        assert list(_compare_eq_cls(Foo(1), Foo(2), util.dummy_highlighter, 2, "ndiff")) == []

    def test_namedtuple(self) -> None:
        from collections import namedtuple

        NT = namedtuple("NT", ["a", "b"])
        expl = callequal(NT(1, 2), NT(1, 3))
        assert expl is not None
        assert len(expl) > 1
