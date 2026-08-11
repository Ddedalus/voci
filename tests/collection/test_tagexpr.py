"""Tests for velox._collection.tagexpr: expression parsing and tag matching."""

from __future__ import annotations

import pytest
from velox._collection.tagexpr import TagExpressionError, compile_tag_expression


@pytest.mark.parametrize(
    "expr, tags, expected",
    [
        ("slow", ("slow",), True),
        ("slow", ("fast",), False),
        ("slow", (), False),
        ("not slow", (), True),
        ("not slow", ("slow",), False),
        ("slow and integration", ("slow", "integration"), True),
        ("slow and integration", ("slow",), False),
        ("slow or integration", ("integration",), True),
        ("slow or integration", (), False),
        ("slow and not flaky", ("slow",), True),
        ("slow and not flaky", ("slow", "flaky"), False),
        ("(slow or integration) and not flaky", ("integration", "flaky"), False),
        ("(slow or integration) and not flaky", ("integration",), True),
    ],
)
def test_matches_evaluates_the_boolean_expression(
    expr: str, tags: tuple[str, ...], expected: bool
) -> None:
    assert compile_tag_expression(expr).matches(tags) is expected


def test_matches_ignores_tags_not_named_in_the_expression() -> None:
    """A tag the test carries but the expression never mentions has no bearing on the result."""
    assert compile_tag_expression("slow").matches(("slow", "unrelated")) is True


def test_raw_preserves_the_original_expression_text() -> None:
    assert compile_tag_expression("slow and not flaky").raw == "slow and not flaky"


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "slow(",
        "slow ==",
    ],
)
def test_a_syntactically_invalid_expression_raises(expr: str) -> None:
    with pytest.raises(TagExpressionError, match="invalid -m expression"):
        compile_tag_expression(expr)


@pytest.mark.parametrize(
    "expr",
    [
        "slow()",
        "slow.upper",
        "slow == 'slow'",
        "slow if True else 'x'",
        "[slow]",
        "1",
        "'slow'",
    ],
)
def test_syntax_outside_names_and_and_or_not_is_rejected(expr: str) -> None:
    """Only tag names combined with and/or/not/parens are accepted -- calls, attribute access,
    comparisons, and literals are all rejected rather than silently evaluated."""
    with pytest.raises(TagExpressionError, match="invalid -m expression"):
        compile_tag_expression(expr)
