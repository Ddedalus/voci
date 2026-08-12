"""Boolean `-m` selection over `@velox.tag` names.

`compile_tag_expression` turns a string like `"slow and not flaky"` into a `TagExpression`: a
predicate that tests one test's tags against the expression. A tag name is a bare identifier, or
a quoted string for a name that isn't one (`'smoke.fast'`, `'-slow'`), combined with `and`,
`or`, `not`, and parentheses -- anything else raises `TagExpressionError` naming what was found
instead of a bare `SyntaxError`.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["TagExpression", "TagExpressionError", "compile_tag_expression"]


class TagExpressionError(ValueError):
    """`-m EXPR` where `EXPR` is not a boolean combination of tag names."""


# What compile_tag_expression allows through: a tag name (a bare identifier, or a quoted string
# for a name that isn't a legal identifier) combined with `and`/`or`/`not` and parentheses. No
# calls, attribute access, comparisons, or non-string literals.
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
        return _evaluate(self._tree.body, frozenset(tags))


def compile_tag_expression(expr: str) -> TagExpression:
    """Parse `expr` into a `TagExpression`. Raises `TagExpressionError` if `expr` is not a
    boolean combination of tag names -- e.g. it contains a call, a comparison, a non-string
    literal, or is empty."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise _invalid(expr, exc.msg or "syntax error") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise _invalid(expr, f"not allowed: {type(node).__name__}")
        if isinstance(node, ast.Constant) and not isinstance(node.value, str):
            raise _invalid(expr, f"not a quoted tag name: {node.value!r}")
    return TagExpression(raw=expr, _tree=tree)


def _invalid(expr: str, detail: str) -> TagExpressionError:
    return TagExpressionError(
        f"invalid -m expression {expr!r}: {detail} -- only tag names (bare identifiers, or "
        f"quoted strings for names that aren't), 'and', 'or', 'not', and parentheses are allowed"
    )


def _evaluate(node: ast.expr, tags: frozenset[str]) -> bool:
    """Walks `node`, a tree already vetted by `compile_tag_expression`, matching each tag
    reference -- `ast.Name` or a string `ast.Constant` alike -- against `tags`."""
    if isinstance(node, ast.Name):
        return node.id in tags
    if isinstance(node, ast.Constant):
        return node.value in tags
    if isinstance(node, ast.UnaryOp):
        return not _evaluate(node.operand, tags)
    if isinstance(node, ast.BoolOp):
        results = (_evaluate(value, tags) for value in node.values)
        return any(results) if isinstance(node.op, ast.Or) else all(results)
    raise TypeError(f"unreachable: {type(node).__name__} passed compile_tag_expression's checks")
