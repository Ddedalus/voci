"""The wiring swap: names pytest resolved at run time become objects imported at import time.

This is the one rewrite that has no pytest-to-pytest equivalent, and the reason `convert` exists.
A fixture stops being a function pytest looks up by name and becomes a module-level object; every
parameter that requested it by name grows a `Depends()` default naming that object; and the import
statement carrying the object into scope is the wiring itself.

Two constraints from how velox reads injection shape the output. It reads `__defaults__`, so a
`Depends()` has to sit in default position — which means a parameter without a default can never
follow one that has it, and a signature mixing injected and parametrized names is reordered. And
it binds every argument by keyword, so the order it is reordered into is free.

Signature whitespace is preserved wherever the original order already works, which is every
signature whose parameters are all injected — the common case.

A signature also grows and loses parameters here. A dependency a body asked for by name arrives
as an injection with no parameter of its own and becomes one; a `request` whose every use the body
rules rewrote arrives as an injection with no parameter left and stops being one.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

import libcst as cst
from libcst.codemod import CodemodContext, VisitorBasedCodemodCommand
from libcst.codemod.visitors import AddImportsVisitor, RemoveImportsVisitor

from velox_migrate import model
from velox_migrate.convert.plan import FileWork, FixtureWork, Injection, TestWork
from velox_migrate.model import REQUEST

# The case argument velox binds a parametrized fixture's value to; there is no `request`.
PARAM = "param"

CAPLOG = "caplog"
SET_LEVEL = "set_level"

# The row that refuses a definition whose body still reads a renamed builtin. `caplog` is decided
# per use, so it is not here.
_STRANDED: Mapping[str, str] = {"capsys": "VX202"}

_NO_SPACE = cst.SimpleWhitespace("")


@dataclass(frozen=True, slots=True)
class Result:
    """The rewritten module, and the definitions it turned out not to be safe to rewrite.

    `refused` names each definition left as pytest wrote it, against the support-matrix code that
    says why — a `request` a rewrite could not eliminate, or an injection in a position velox
    cannot bind.
    """

    module: cst.Module
    refused: tuple[tuple[str, str], ...] = ()


def apply(
    module: cst.Module, work: FileWork, *, needs: Collection[str] = (), touched: bool = False
) -> Result:
    """`work`'s fixture and test rewrites, plus the imports they need, applied to `module`.

    `needs` are plain modules a rule's rewrite left the source reading — it writes no import of its
    own, and imports belong to the one pass that owns them. `touched` says a rule already wrote a
    `velox.` name into this module, which is what makes the import of `velox` needed even where
    every signature here turns out to be refused.
    """
    command = _Wiring(CodemodContext(), work, needs, touched)
    rewritten = command.transform_module(module)
    return Result(module=rewritten, refused=tuple(sorted(command.refused)))


def tidy(module: cst.Module) -> cst.Module:
    """`module` without an `import pytest` nothing in it references any more.

    Run after the rules rather than with the wiring swap: a mark still spelled `pytest.mark.slow`
    when the signatures were rewritten is a velox tag by the time the last rule has been through,
    and only then is the import genuinely unused.
    """
    return _Tidy(CodemodContext()).transform_module(module)


class _Tidy(VisitorBasedCodemodCommand):
    """Queues the removal of the suite's `import pytest`, which happens only if nothing needs it."""

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
        RemoveImportsVisitor.remove_unused_import(self.context, "pytest")
        return updated_node


class _Wiring(VisitorBasedCodemodCommand):
    """Rewrites one file's fixture definitions and test signatures, and queues its imports."""

    def __init__(
        self,
        context: CodemodContext,
        work: FileWork,
        needs: Collection[str] = (),
        touched: bool = False,
    ) -> None:
        super().__init__(context)
        self._fixtures: Mapping[str, FixtureWork] = {f.symbol: f for f in work.fixtures}
        self._tests: Mapping[str, TestWork] = {t.qualname: t for t in work.tests}
        self._needs = sorted(set(needs))
        self._work = work
        self._path: list[str] = []
        self._wrote = touched
        self._injected = False
        self.refused: set[tuple[str, str]] = set()

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
        name = original_node.name.value
        fixture = self._fixtures.get(name) if len(self._path) == 0 else None
        work = fixture or self._tests.get(qualname)
        if work is None:
            return updated_node
        stranded = _stranded(updated_node, work.injections)
        if stranded is not None:
            # Renaming the parameter would leave the body reading a name nothing binds. The rules
            # rewrite every use of `capsys` and `caplog` they have a velox spelling for, so a use
            # still standing here is one of the shapes they declined.
            self.refused.add((name, stranded))
            return original_node
        if fixture is not None:
            self._wrote = True
            return self._fixture(original_node, updated_node, fixture)
        return self._signature(updated_node, work.injections, name)

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
        # Imported on what was written, not on what was planned: a file whose every definition
        # turned out to be refused keeps the source it had, and an unused import is not an edit
        # anyone asked for.
        if self._wrote:
            AddImportsVisitor.add_needed_import(self.context, "velox")
        if self._injected:
            AddImportsVisitor.add_needed_import(self.context, "velox", "Depends")
        for module in self._needs:
            AddImportsVisitor.add_needed_import(self.context, module)
        for wanted in self._work.imports:
            AddImportsVisitor.add_needed_import(
                self.context, wanted.module, wanted.symbol, wanted.alias
            )
        return updated_node

    def _fixture(
        self,
        original: cst.FunctionDef,
        updated: cst.FunctionDef,
        work: FixtureWork,
    ) -> cst.FunctionDef:
        if not _request_is_only_param(original):
            self.refused.add((work.symbol, "VX015"))
            return original
        decorators = [
            d.with_changes(decorator=_fixture_call(d.decorator, work))
            if _is_fixture_decorator(d.decorator)
            else d
            for d in updated.decorators
        ]
        rewritten = self._signature(
            updated.with_changes(decorators=decorators), work.injections, work.symbol
        )
        if work.request_param is None:
            return rewritten
        return rewritten.with_changes(body=rewritten.body.visit(_RequestParam()))

    def _signature(
        self, node: cst.FunctionDef, injections: Sequence[Injection], name: str
    ) -> cst.FunctionDef:
        if not injections:
            return node
        by_name = {injection.was: injection for injection in injections}
        if any(param.name.value in by_name for param in node.params.posonly_params):
            # velox binds every argument by keyword, so a positional-only parameter can never be
            # injected. Rewriting the signature would change what the test can be called with.
            self.refused.add((name, "VX017"))
            return node
        self._wrote = True
        self._injected = self._injected or any(injection.was != REQUEST for injection in injections)
        added = [
            _param(injection)
            for injection in injections
            if injection.asked and injection.was not in _declared(node.params)
        ]
        # A parameter after `*args` is keyword-only, so that is where a new one goes in a signature
        # that has one; every other signature grows it at the end, where a default belongs.
        keyword_only = node.params.star_arg is not cst.MaybeSentinel.DEFAULT
        params, reordered = _rewrite(node.params.params, by_name, () if keyword_only else added)
        kwonly, _ = _rewrite(node.params.kwonly_params, by_name, added if keyword_only else ())
        if reordered or added:
            params = tuple(param.with_changes(comma=cst.MaybeSentinel.DEFAULT) for param in params)
        return node.with_changes(
            params=node.params.with_changes(params=params, kwonly_params=kwonly)
        )


def _rewrite(
    params: Sequence[cst.Param],
    by_name: Mapping[str, Injection],
    added: Sequence[cst.Param] = (),
) -> tuple[tuple[cst.Param, ...], bool]:
    """`params` with each injected one given its `Depends()` default, and whether order moved.

    A parameter without a default cannot follow one that has it, so a signature that mixes
    injected names with parametrized ones is split: the ones without a default keep their relative
    order and come first. Every other signature keeps the whitespace it was written with.

    What decides the split is whether the rewritten parameter ends up with a default, not whether
    it was injected: a `params=` fixture's `request` becomes the bare `param` velox binds its case
    to, so it belongs with the plain parameters however late it was written. `added` are the
    parameters a body asked for by name, which are injected and so go last either way.
    """
    kept = [param for param in params if not _is_dropped(param, by_name)]
    rewritten = [_inject(param, by_name.get(param.name.value)) for param in kept]
    rewritten += list(added)
    defaulted = [param.default is not None for param in rewritten]
    if all(defaulted) or not any(defaulted) or defaulted == sorted(defaulted):
        return tuple(rewritten), False
    plain = [param for param, has in zip(rewritten, defaulted, strict=True) if not has]
    filled = [param for param, has in zip(rewritten, defaulted, strict=True) if has]
    return tuple([*plain, *filled]), True


def _is_dropped(param: cst.Param, by_name: Mapping[str, Injection]) -> bool:
    """Whether this parameter is one the rewrite takes away rather than binds."""
    injection = by_name.get(param.name.value)
    return injection is not None and injection.dropped


def _declared(params: cst.Parameters) -> frozenset[str]:
    """Every name this signature already binds, wherever in it the name was written."""
    named = {
        param.name.value
        for param in (*params.posonly_params, *params.params, *params.kwonly_params)
    }
    for star in (params.star_arg, params.star_kwarg):
        if isinstance(star, cst.Param):
            named.add(star.name.value)
    return frozenset(named)


def _param(injection: Injection) -> cst.Param:
    """The parameter a dependency asked for by name becomes: injected, and written with a comma."""
    return cst.Param(
        name=cst.Name(injection.param),
        default=cst.Call(
            func=cst.Name("Depends"),
            args=[cst.Arg(value=cst.parse_expression(injection.reference))],
        ),
        equal=cst.AssignEqual(whitespace_before=_NO_SPACE, whitespace_after=_NO_SPACE),
        comma=cst.MaybeSentinel.DEFAULT,
    )


def _inject(param: cst.Param, injection: Injection | None) -> cst.Param:
    if injection is None:
        return param
    if injection.was == REQUEST:
        return param.with_changes(name=cst.Name(PARAM), annotation=None, default=None)
    return param.with_changes(
        name=cst.Name(injection.param),
        default=cst.Call(
            func=cst.Name("Depends"),
            args=[cst.Arg(value=cst.parse_expression(injection.reference))],
        ),
        equal=cst.AssignEqual(whitespace_before=_NO_SPACE, whitespace_after=_NO_SPACE)
        if param.annotation is None
        else cst.MaybeSentinel.DEFAULT,
    )


def _stranded(node: cst.FunctionDef, injections: Sequence[Injection]) -> str | None:
    """The matrix row refusing this definition, if renaming a parameter would strand its body.

    A builtin whose velox counterpart is a different object is renamed with the parameter, and
    every use the rules could translate is already translated by the time this runs. What is left
    is a use with no velox spelling, and the row that says so depends on which one it is.
    """
    for injection in injections:
        if injection.dropped:
            # Every use of `request` here was one a rule rewrote — unless one of them was written
            # in a shape the rule declined, in which case the body still reads a parameter that is
            # about to go away.
            if _mentions(node.body, REQUEST):
                return "VX015"
            continue
        if not injection.renamed or injection.was == REQUEST:
            continue
        if not _mentions(node.body, injection.was):
            continue
        if injection.was == CAPLOG:
            return "VX205" if _mentions(node.body, SET_LEVEL) else "VX206"
        return _STRANDED.get(injection.was, "VX220")
    return None


def _mentions(body: cst.BaseSuite, name: str) -> bool:
    visitor = _Mentions(name)
    body.visit(visitor)
    return visitor.found


class _Mentions(cst.CSTVisitor):
    """Whether a name is read anywhere in a body, as a bare name or an attribute's owner."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name
        self.found = False

    def visit_Name(self, node: cst.Name) -> None:
        if node.value == self._name:
            self.found = True


def _is_fixture_decorator(expression: cst.BaseExpression) -> bool:
    """Whether this decorator is a `@pytest.fixture`, however `pytest` was spelled.

    Matched by the attribute name rather than by a resolved qualified name, so that this and the
    planning pass that named the fixture agree about which definitions are fixtures. `velox.fixture`
    is excluded, which is what makes rewriting an already-rewritten file a no-op.
    """
    target = expression.func if isinstance(expression, cst.Call) else expression
    match target:
        case cst.Attribute(value=cst.Name(value="velox"), attr=cst.Name(value="fixture")):
            return False
        case cst.Attribute(attr=cst.Name(value="fixture")):
            return True
        case cst.Name(value="fixture"):
            return True
        case _:
            return False


def _fixture_call(expression: cst.BaseExpression, work: FixtureWork) -> cst.BaseExpression:
    """`@velox.fixture(...)`, carrying over the arguments that still mean something.

    `scope` is written only where it is not velox's default, `params` and `ids` come across
    verbatim, and `autouse` has no counterpart — a declaration replaces it, which is why an
    autouse fixture never reaches here.
    """
    existing = {
        arg.keyword.value: arg
        for arg in (expression.args if isinstance(expression, cst.Call) else ())
        if arg.keyword is not None
    }
    args: list[cst.Arg] = []
    if work.scope != "function":
        args.append(_kwarg("scope", cst.SimpleString(f'"{work.scope}"')))
    for keyword in ("name", "params", "ids"):
        carried = existing.get(keyword)
        if carried is not None:
            args.append(_kwarg(keyword, carried.value))
    return cst.Call(
        func=cst.Attribute(value=cst.Name("velox"), attr=cst.Name("fixture")),
        args=[arg.with_changes(comma=cst.MaybeSentinel.DEFAULT) for arg in args],
    )


def _kwarg(keyword: str, value: cst.BaseExpression) -> cst.Arg:
    return cst.Arg(
        value=value,
        keyword=cst.Name(keyword),
        equal=cst.AssignEqual(whitespace_before=_NO_SPACE, whitespace_after=_NO_SPACE),
    )


class _RequestParam(cst.CSTTransformer):
    """Rewrites `request.param` to the `param` argument velox binds the case to."""

    def leave_Attribute(
        self, original_node: cst.Attribute, updated_node: cst.Attribute
    ) -> cst.BaseExpression:
        match updated_node:
            case cst.Attribute(value=cst.Name(value=model.REQUEST), attr=cst.Name(value="param")):
                return cst.Name(PARAM)
            case _:
                return updated_node


class _RequestUses(cst.CSTVisitor):
    """Records every use of a `request` parameter that is not `request.param`."""

    def __init__(self) -> None:
        super().__init__()
        self.other = False

    def visit_Attribute(self, node: cst.Attribute) -> bool:
        match node:
            case cst.Attribute(value=cst.Name(value=model.REQUEST), attr=cst.Name(value="param")):
                return False
            case cst.Attribute(value=cst.Name(value=model.REQUEST)):
                self.other = True
                return False
            case _:
                return True

    def visit_Name(self, node: cst.Name) -> None:
        if node.value == REQUEST:
            self.other = True


def _request_is_only_param(node: cst.FunctionDef) -> bool:
    """Whether a fixture's body reads nothing from `request` except its own case.

    The audit refuses every other `request` use it can name, and this is the backstop: a use it
    did not name would otherwise be rewritten into a fixture referring to a parameter that is no
    longer there.
    """
    if not any(param.name.value == REQUEST for param in node.params.params):
        return True
    visitor = _RequestUses()
    node.body.visit(visitor)
    return not visitor.other
