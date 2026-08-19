"""The tests velox runs by itself, and the decorator that says so.

`unittest.mock` installs a patch by writing to a module or a class, which every test running at
the same time sees. A patch applied as a decorator is on the function object, so velox finds it at
collection and schedules that test alone without being told. A patch entered inside a body is not
there to be found until the body runs, so the test carries `@velox.solo` and nothing else runs
while it holds.

Idempotent by reading the decorators a function already carries rather than by tracking what an
earlier run wrote, so a hand-written `@velox.solo` counts the same as a generated one.
"""

from __future__ import annotations

from collections.abc import Collection

import libcst as cst

__all__ = ["apply"]

SOLO = "solo"
VELOX = "velox"


def apply(module: cst.Module, qualnames: Collection[str]) -> cst.Module:
    """`module` with `@velox.solo` above each function `qualnames` names, by qualified name."""
    if not qualnames:
        return module
    return module.visit(_Solo(frozenset(qualnames)))


class _Solo(cst.CSTTransformer):
    """Attaches `@velox.solo` to the functions named, tracking qualified names as it goes."""

    def __init__(self, qualnames: frozenset[str]) -> None:
        super().__init__()
        self._qualnames = qualnames
        self._path: list[str] = []

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        self._path.append(node.name.value)
        return True

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        self._path.append(node.name.value)
        return True

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef):
        self._path.pop()
        return updated_node

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        qualname = ".".join(self._path)
        self._path.pop()
        if qualname not in self._qualnames or _already_solo(updated_node):
            return updated_node
        # Outermost, where a reader meets it first: velox reads a test's marks through whatever
        # decorators wrap it, so nothing about the order changes what it schedules. The blank lines
        # above the function stay on the function, which is where LibCST keeps them.
        return updated_node.with_changes(decorators=[_decorator(), *updated_node.decorators])


def _already_solo(node: cst.FunctionDef) -> bool:
    return any(_is_solo(decorator.decorator) for decorator in node.decorators)


def _is_solo(expression: cst.BaseExpression) -> bool:
    match expression:
        case cst.Attribute(value=cst.Name(value="velox"), attr=cst.Name(value="solo")):
            return True
        case _:
            return False


def _decorator() -> cst.Decorator:
    return cst.Decorator(decorator=cst.Attribute(value=cst.Name(VELOX), attr=cst.Name(SOLO)))
