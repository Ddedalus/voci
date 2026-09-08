"""Boolean selection expressions: `-m` over `@voci.tag` names, `-k` over test ids.

Both flags take the same grammar -- terms combined with `and`, `or`, `not` and parentheses --
and differ only in what a term means. `compile_tag_expression` builds a `TagExpression`, whose
terms are tag names a test either carries or doesn't; `compile_keyword_expression` builds a
`KeywordExpression`, whose terms are case-insensitive substrings of a test's id. A term is a
bare identifier, or a quoted string for a term that isn't one (`'smoke.fast'`, `'-slow'`,
`'test_x[case-1]'`); anything else raises `SelectionError` naming what was found instead of a
bare `SyntaxError`.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable
from dataclasses import dataclass

__all__ = [
    "KeywordExpression",
    "SelectionError",
    "TagExpression",
    "compile_keyword_expression",
    "compile_tag_expression",
]


class SelectionError(ValueError):
    """A `-m`/`-k` EXPR that is not a boolean combination of terms."""


# What _parse allows through: a term (a bare identifier, or a quoted string for a term that is
# not a legal identifier) combined with `and`/`or`/`not` and parentheses. No calls, attribute
# access, comparisons, or non-string literals.
_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.Name,
    ast.Load,
    ast.Constant,
)


@dataclass(frozen=True, slots=True)
class TagExpression:
    """A compiled `-m` expression. Call `matches` with a test's tags to test selection."""

    raw: str
    _tree: ast.Expression

    def matches(self, tags: Iterable[str]) -> bool:
        present = frozenset(tags)
        return _evaluate(self._tree.body, lambda term: term in present)


@dataclass(frozen=True, slots=True)
class KeywordExpression:
    """A compiled `-k` expression. Call `matches` with a test's id to test selection."""

    raw: str
    _tree: ast.Expression

    def matches(self, test_id: str) -> bool:
        """Whether `test_id` satisfies the expression, each term matched as a case-insensitive
        substring of the whole id -- path, function name, and `[case]` suffix alike, so
        `-k users` selects everything in `tests/test_users.py` and `-k admin` selects the
        `[admin]` case of a parametrized test."""
        lowered = test_id.lower()
        return _evaluate(self._tree.body, lambda term: term.lower() in lowered)


def compile_tag_expression(expr: str) -> TagExpression:
    """Parse `expr` into a `TagExpression`. Raises `SelectionError` if `expr` is not a boolean
    combination of tag names -- e.g. it contains a call, a comparison, a non-string literal, or
    is empty."""
    return TagExpression(raw=expr, _tree=_parse(expr, option="-m", terms="tag names"))


def compile_keyword_expression(expr: str) -> KeywordExpression:
    """Parse `expr` into a `KeywordExpression`. Raises `SelectionError` on the same shapes
    `compile_tag_expression` rejects."""
    return KeywordExpression(raw=expr, _tree=_parse(expr, option="-k", terms="id substrings"))


def _parse(expr: str, *, option: str, terms: str) -> ast.Expression:
    """The vetted syntax tree behind both flags. `option`/`terms` only shape the error message,
    so a rejected `-k` expression is never explained in terms of `-m`."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise _invalid(expr, exc.msg or "syntax error", option=option, terms=terms) from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise _invalid(expr, f"not allowed: {type(node).__name__}", option=option, terms=terms)
        if isinstance(node, ast.Constant) and not isinstance(node.value, str):
            raise _invalid(expr, f"not a quoted term: {node.value!r}", option=option, terms=terms)
    return tree


def _invalid(expr: str, detail: str, *, option: str, terms: str) -> SelectionError:
    return SelectionError(
        f"invalid {option} expression {expr!r}: {detail} -- only {terms} (bare identifiers, or "
        f"quoted strings for terms that aren't), 'and', 'or', 'not', and parentheses are allowed"
    )


def _evaluate(node: ast.expr, term_matches: Callable[[str], bool]) -> bool:
    """Walks `node`, a tree already vetted by `_parse`, handing each term -- `ast.Name` or a
    string `ast.Constant` alike -- to `term_matches`, which is what separates `-m`'s membership
    test from `-k`'s substring test."""
    if isinstance(node, ast.Name):
        return term_matches(node.id)
    # The `str` check is `_parse`'s guarantee restated for the type checker: every other kind
    # of constant was already rejected, so a non-string one reaching here would be a bug in
    # `_parse`, which is what the `raise` at the bottom says.
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return term_matches(node.value)
    if isinstance(node, ast.UnaryOp):
        return not _evaluate(node.operand, term_matches)
    if isinstance(node, ast.BoolOp):
        results = (_evaluate(value, term_matches) for value in node.values)
        return any(results) if isinstance(node.op, ast.Or) else all(results)
    raise TypeError(f"unreachable: {type(node).__name__} passed _parse's checks")
