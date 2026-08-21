"""VX024: the cases a hook built while pytest collected, written out as the parametrize that
lists them.

`pytest_generate_tests` runs during collection, so the cases it produced are in the dump and the
hook itself has nothing to become — velox reads a test's cases off the test. This rule writes the
frozen list the plan read out of the dump, one decorator per axis, outermost first: velox reads
its own decorators outermost-first and pytest composed its ids in the same order, so the node id
of every case is where it was.

The one rule here writes a decorator no source form asked for, so what makes it idempotent is the
decorator itself: a test already carrying a `@velox.parametrize` over the same argnames is one an
earlier run wrote, and it is left exactly as it is.
"""

from __future__ import annotations

from collections.abc import Sequence

import libcst as cst

from velox_migrate.convert import parametrize
from velox_migrate.convert.parametrize import Generated
from velox_migrate.convert.rules import (
    Rule,
    RuleTransformer,
    argument,
    dotted,
    literal_text,
    rule,
    string,
    velox,
)


class _Generated(RuleTransformer):
    """VX024: a `@velox.parametrize` per axis a hook produced, on each test it produced them for."""

    CODE = "VX024"

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        wanted = self.context.generated.get(self.qualname or "")
        if not wanted or self.is_blocked or not self.is_test:
            return updated_node
        written = _written(updated_node)
        missing = [axis for axis in wanted if axis.key not in written]
        if not missing:
            return updated_node
        for axis in missing:
            self.record(
                f"`{axis.key}` is parametrized by a hook, and its "
                f"{len(axis.values)} case(s) are written out as `@velox.parametrize`.",
                code="VX024" if axis.ids is not None else "VX114",
            )
        return updated_node.with_changes(
            decorators=[*(_decorator(axis) for axis in missing), *updated_node.decorators]
        )


def _written(node: cst.FunctionDef) -> frozenset[str]:
    """The axes this function already carries a `@velox.parametrize` for, by their argnames."""
    found: set[str] = set()
    for decorator in node.decorators:
        call = decorator.decorator
        if not isinstance(call, cst.Call) or dotted(call.func) != "velox.parametrize":
            continue
        spelled = literal_text(call.args[0].value) if call.args else None
        if spelled is not None:
            found.add(spelled)
    return frozenset(found)


def _decorator(axis: Generated) -> cst.Decorator:
    args = [argument(string(axis.key)), argument(_cases(axis))]
    if axis.ids is not None:
        args.append(argument(cst.List([cst.Element(string(text)) for text in axis.ids]), "ids"))
    return cst.Decorator(decorator=cst.Call(func=velox("parametrize"), args=args))


def _cases(axis: Generated) -> cst.List:
    """The case list: one value per case for a single argname, and a tuple per case for several."""
    return cst.List([cst.Element(_case(row)) for row in axis.values])


def _case(row: Sequence[str]) -> cst.BaseExpression:
    values = [_value(text) for text in row]
    if len(values) == 1:
        return values[0]
    return cst.Tuple([cst.Element(value) for value in values])


def _value(text: str) -> cst.BaseExpression:
    value = parametrize.literal(text)
    assert value is not None, text
    return value


RULES: tuple[Rule, ...] = (rule(_Generated),)
