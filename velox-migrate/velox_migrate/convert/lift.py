"""Fixtures a test class wrote, moved out to the module level.

pytest let a class own fixtures, visible to its methods and to nothing else. A velox test class is
pure namespacing — it has no lifecycle and no fixtures of its own — so each of those factories
becomes an ordinary module-level object, under a name carrying the class's so that two classes
writing a `schema` stay two objects.

The move runs before every other rewrite and leaves the pytest spelling untouched, so the copies,
the rules and the wiring swap all see a module-level fixture and need to know nothing about where
it was written. Each factory lands immediately above the class it came from, because a `Depends()`
default in a method's signature is evaluated while the class body runs: a fixture written below
the class would be a name nothing has bound yet.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import libcst as cst

from velox_migrate.convert.plan import FixtureWork

__all__ = ["apply"]

# The first parameter of a method, which a module-level function has no use for.
_RECEIVERS = ("self", "cls")


def apply(module: cst.Module, fixtures: Sequence[FixtureWork]) -> cst.Module:
    """`module` with every fixture in `fixtures` that names a class lifted out of it."""
    wanted: dict[str, list[FixtureWork]] = {}
    for fixture in fixtures:
        if fixture.lift is not None:
            wanted.setdefault(fixture.lift, []).append(fixture)
    if not wanted:
        return module

    body: list[cst.BaseStatement] = []
    for statement in module.body:
        found = wanted.get(statement.name.value) if isinstance(statement, cst.ClassDef) else None
        if found is None or not isinstance(statement, cst.ClassDef):
            body.append(statement)
            continue
        emptied, lifted = _drained(statement, {work.written: work for work in found})
        body.extend(_definition(node) for node in _ordered(lifted, found))
        body.append(emptied)
    return module.with_changes(body=body)


def _drained(
    node: cst.ClassDef, wanted: Mapping[str, FixtureWork]
) -> tuple[cst.ClassDef, dict[str, cst.FunctionDef]]:
    """`node` without the factories `wanted` names, and each of them re-bound to its new name.

    A class written on one line holds no `def`, so there is nothing in it to lift.
    """
    lifted: dict[str, cst.FunctionDef] = {}
    kept: list[cst.BaseStatement] = []
    if not isinstance(node.body, cst.IndentedBlock):
        return node, lifted
    for statement in node.body.body:
        work = wanted.get(statement.name.value) if isinstance(statement, cst.FunctionDef) else None
        if work is None or not isinstance(statement, cst.FunctionDef):
            kept.append(statement)
            continue
        rebound = statement.with_changes(
            name=cst.Name(work.symbol), params=_without_receiver(statement.params)
        )
        through = rebound.visit(_ThroughClass(node.name.value))
        assert isinstance(through, cst.FunctionDef)
        lifted[work.symbol] = through
    if not kept:
        # A class that wrote nothing but fixtures still names a group velox collects tests under,
        # and an empty suite is not something this is entitled to delete.
        kept = [cst.SimpleStatementLine(body=[cst.Pass()])]
    return node.with_changes(body=node.body.with_changes(body=kept)), lifted


class _ThroughClass(cst.CSTTransformer):
    """Rewrites `self.attribute` to read it through the class the factory is leaving.

    Only an attribute the class body itself binds is ever read this way here: the audit refuses
    every other `self` under `VX033` before a fixture reaches this, so a `self` still standing is
    one the class name answers.

    A `def` nested in the factory that declares a `self` of its own — a method on a class the
    factory builds — is left alone, since that `self` is its own and the move does not touch it.
    """

    def __init__(self, holder: str) -> None:
        super().__init__()
        self._holder = holder
        self._shadowed = 0

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        if _declares_receiver(node):
            self._shadowed += 1
        return True

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        if _declares_receiver(original_node):
            self._shadowed -= 1
        return updated_node

    def leave_Attribute(
        self, original_node: cst.Attribute, updated_node: cst.Attribute
    ) -> cst.Attribute:
        if self._shadowed:
            return updated_node
        if isinstance(updated_node.value, cst.Name) and updated_node.value.value == "self":
            return updated_node.with_changes(value=cst.Name(self._holder))
        return updated_node


def _declares_receiver(node: cst.FunctionDef) -> bool:
    """Whether this `def` binds a `self` of its own, which shadows any `self` above it."""
    return any(param.name.value == "self" for param in node.params.params)


def _without_receiver(params: cst.Parameters) -> cst.Parameters:
    """`params` without the `self` a method was written with, which nothing binds any more."""
    if not params.params or params.params[0].name.value not in _RECEIVERS:
        return params
    remaining = list(params.params[1:])
    if remaining:
        remaining[-1] = remaining[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)
    return params.with_changes(params=remaining)


def _ordered(
    lifted: Mapping[str, cst.FunctionDef], fixtures: Sequence[FixtureWork]
) -> list[cst.FunctionDef]:
    """The lifted factories, each behind the siblings it names.

    One class fixture may depend on another, and once both are module-level objects the one that
    is named has to be written first — a `Depends()` reads the name where the `def` under it is.
    """
    pending = {work.symbol: work for work in fixtures if work.symbol in lifted}
    written: list[cst.FunctionDef] = []
    while pending:
        ready = [
            symbol
            for symbol, work in pending.items()
            if not {injection.reference for injection in work.injections}
            & (set(pending) - {symbol})
        ]
        # A fixture graph is acyclic, so this only guards against writing nothing forever.
        for symbol in ready or list(pending):
            written.append(lifted[symbol])
            del pending[symbol]
    return written


def _definition(node: cst.FunctionDef) -> cst.FunctionDef:
    """A lifted factory spaced from what it follows the way a formatter would space it."""
    return node.with_changes(leading_lines=[cst.EmptyLine(), cst.EmptyLine()])
