"""Tests for velox._collection.selection: expression parsing, tag matching and -k id matching."""

from __future__ import annotations

import pytest

from velox._collection.selection import (
    SelectionError,
    compile_keyword_expression,
    compile_tag_expression,
)


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
    "expr, tags, expected",
    [
        ("'smoke.fast'", ("smoke.fast",), True),
        ("'smoke.fast'", (), False),
        ("'integration-test'", ("integration-test",), True),
        ("'-slow' and not 'flaky.v2'", ("-slow",), True),
        ("'-slow' and not 'flaky.v2'", ("-slow", "flaky.v2"), False),
    ],
)
def test_a_quoted_string_selects_a_tag_name_that_is_not_a_bare_identifier(
    expr: str, tags: tuple[str, ...], expected: bool
) -> None:
    """A tag name with characters `and`/`or`/`not`/bare identifiers can't spell (a hyphen, a
    dot) is still selectable, quoted."""
    assert compile_tag_expression(expr).matches(tags) is expected


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "slow(",
        "slow ==",
    ],
)
def test_a_syntactically_invalid_expression_raises(expr: str) -> None:
    with pytest.raises(SelectionError, match="invalid -m expression"):
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
        "1.5",
        "b'slow'",
        "True",
        "None",
    ],
)
def test_syntax_outside_names_quoted_names_and_and_or_not_is_rejected(expr: str) -> None:
    """Only tag names (bare or quoted) combined with and/or/not/parens are accepted -- calls,
    attribute access, comparisons, and non-string literals are all rejected rather than
    silently evaluated."""
    with pytest.raises(SelectionError, match="invalid -m expression"):
        compile_tag_expression(expr)


_ID = "tests/api/test_users.py::TestDelete::test_create[admin]"


@pytest.mark.parametrize(
    "expr, expected",
    [
        ("users", True),
        ("orders", False),
        ("test_create", True),
        ("TestDelete", True),
        ("not orders", True),
        ("users and create", True),
        ("users and orders", False),
        ("users or orders", True),
        ("(users or orders) and not create", False),
    ],
)
def test_keyword_terms_match_substrings_of_the_whole_id(expr: str, expected: bool) -> None:
    """`-k` matches path, class, function name and case suffix alike -- the whole id is the
    haystack, which is what makes `-k users` mean "everything in tests/test_users.py"."""
    assert compile_keyword_expression(expr).matches(_ID) is expected


def test_keyword_matching_is_case_insensitive() -> None:
    assert compile_keyword_expression("USERS").matches(_ID) is True
    assert compile_keyword_expression("testdelete").matches(_ID) is True


def test_a_quoted_keyword_term_selects_a_parametrize_case() -> None:
    """`[admin]` isn't a bare identifier, so quoting is how a case id is named."""
    assert compile_keyword_expression("'test_create[admin]'").matches(_ID) is True
    assert compile_keyword_expression("'test_create[guest]'").matches(_ID) is False


@pytest.mark.parametrize("expr", ["", "users and", "users()", "1"])
def test_an_invalid_keyword_expression_is_reported_as_a_k_error(expr: str) -> None:
    """The message names the flag that was actually given -- a rejected `-k` expression
    explained in terms of `-m` would send the reader to the wrong place."""
    with pytest.raises(SelectionError, match="invalid -k expression"):
        compile_keyword_expression(expr)
