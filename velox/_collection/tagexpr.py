"""Boolean `-m` selection over `@velox.tag` names.

`compile_tag_expression` turns a string like `"slow and not flaky"` into a `TagExpression`: a
compiled predicate that tests one test's tags against the expression. Only tag names, `and`,
`or`, `not`, and parentheses are accepted -- anything else raises `TagExpressionError` naming
what was found instead, rather than a bare `SyntaxError`.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from types import CodeType

__all__ = ["TagExpression", "TagExpressionError", "compile_tag_expression"]


class TagExpressionError(ValueError):
    """`-m EXPR` where `EXPR` is not a boolean combination of tag names."""


# What compile_tag_expression allows through: an expression built only from names combined with
# `and`/`or`/`not` and parentheses -- no calls, attributes, comparisons, or literals. This is the
# whitelist eval() below relies on for safety, not just a style preference.
_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.Name,
    ast.Load,
)


@dataclass(frozen=True, slots=True)
class TagExpression:
    """A compiled `-m` expression. Call `matches` with a test's tags to test selection."""

    raw: str
    _code: CodeType
    _names: tuple[str, ...]

    def matches(self, tags: Iterable[str]) -> bool:
        present = frozenset(tags)
        env = {name: name in present for name in self._names}
        # Safe despite eval(): compile_tag_expression already rejected every node except
        # names, and/or/not, and parentheses, so there is no call or attribute access this
        # expression could perform.
        return bool(eval(self._code, {"__builtins__": {}}, env))


def compile_tag_expression(expr: str) -> TagExpression:
    """Parse `expr` into a `TagExpression`. Raises `TagExpressionError` if `expr` is not a
    boolean combination of bare tag names -- e.g. it contains a call, a comparison, or is
    empty."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise TagExpressionError(f"invalid -m expression {expr!r}: {exc.msg}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise TagExpressionError(
                f"invalid -m expression {expr!r}: only tag names, 'and', 'or', 'not', and "
                f"parentheses are allowed, not {type(node).__name__}"
            )
    names = tuple(sorted({node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}))
    code = compile(tree, "<tag-expression>", "eval")
    return TagExpression(raw=expr, _code=code, _names=names)
