"""The wiring swap: names pytest resolved at run time become objects imported at import time.

This is the one rewrite that has no pytest-to-pytest equivalent, and the reason `convert` exists.
A fixture stops being a function pytest looks up by name and becomes a module-level object; every
parameter that requested it by name is annotated `Annotated[T, Depends(object)]`; and the import
statement carrying the object into scope is the wiring itself.

velox reads an injection out of the parameter's annotation metadata, so nothing a signature grows
carries a default and the order the source was written in stands. The one order that moves is a
parameter a body asked for by name landing after one the source gave a default, since a parameter
without a default cannot follow one that has it — and velox binds every argument by keyword, so
the order it moves into is free. Every other signature keeps the whitespace it was written with.

A signature also grows and loses parameters here. A dependency a body asked for by name arrives
as an injection with no parameter of its own and becomes one; a `request` whose every use the body
rules rewrote arrives as an injection with no parameter left and stops being one. The type inside
the `Annotated[...]` is the one `convert/annotate.py` inferred from the fixture factory the
parameter is injected from, or `Any` where nothing could be inferred — except where the source
annotated the parameter itself, which is the author's answer to the same question.

Writing a type is why this also writes `from __future__ import annotations`, into every module it
annotates. An annotation is otherwise an expression evaluated when the `def` is read, so a type
named only by a `TYPE_CHECKING` import would be a `NameError` at import time. velox reads the
annotation under either spelling by parsing its source text for the `Depends()` marker, evaluating
that marker and nothing else, as `velox/_di/fixtures.py` says at the top.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field

import libcst as cst
from libcst.codemod import CodemodContext, VisitorBasedCodemodCommand
from libcst.codemod.visitors import AddImportsVisitor, RemoveImportsVisitor

from velox_migrate import model
from velox_migrate.convert import parametrize
from velox_migrate.convert.annotate import TypeImport, statements
from velox_migrate.convert.plan import FileWork, FixtureWork, Injection, TestWork
from velox_migrate.inference import bindings, free
from velox_migrate.model import REQUEST

# The case argument velox binds a parametrized fixture's value to; there is no `request`.
PARAM = "param"

# The two `typing` names a written signature spells itself through.
ANNOTATED = "Annotated"
ANY = "Any"

CAPLOG = "caplog"
SET_LEVEL = "set_level"

# The row that refuses a definition whose body still reads a renamed builtin. `caplog` is decided
# per use, so it is not here.
_STRANDED: Mapping[str, str] = {"capsys": "VX202"}

_NO_SPACE = cst.SimpleWhitespace("")


@dataclass(slots=True)
class _Claims:
    """What the signatures written so far need of the module around them.

    `imports` are the ones a written annotation is spelled through, `replaced` are the annotations
    this pass overwrote, whose own imports may now have no reader left, and `untyped` says one of
    the written signatures reached for `Any`. All three are filled as parameters are written
    rather than when the plan was made, so a definition the rewrite backs out of neither adds an
    import nor takes one away.
    """

    imports: list[TypeImport] = field(default_factory=list)
    replaced: list[cst.Annotation] = field(default_factory=list)
    untyped: bool = False


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
    # After the codemod rather than inside it, so that the block lands below whatever imports
    # `AddImportsVisitor` has just written at the top of the module.
    rewritten = _type_checking(rewritten, command.claims.imports)
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
        self.claims = _Claims()
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
            AddImportsVisitor.add_needed_import(self.context, "typing", ANNOTATED)
        if self.claims.untyped:
            AddImportsVisitor.add_needed_import(self.context, "typing", ANY)
        if self.claims.imports:
            # Every annotation this wrote is a string under the future import, which is what makes
            # naming a type through a `TYPE_CHECKING`-only import safe: an annotation is otherwise
            # an expression evaluated when the `def` is read.
            AddImportsVisitor.add_needed_import(self.context, "__future__", "annotations")
            AddImportsVisitor.add_needed_import(self.context, "typing", "TYPE_CHECKING")
        for module in self._needs:
            AddImportsVisitor.add_needed_import(self.context, module)
        for wanted in self._work.imports:
            AddImportsVisitor.add_needed_import(
                self.context, wanted.module, wanted.symbol, wanted.alias
            )
        # An annotation this pass replaced took its import's last use with it -- `capsys` was the
        # only thing in the file named `CaptureFixture`. Queued rather than cut: the visitor
        # removes an import only if nothing else in the module still references it.
        for item in self._orphaned(original_node):
            RemoveImportsVisitor.remove_unused_import(
                self.context, item.module, item.symbol, item.alias
            )
        return updated_node

    def _orphaned(self, original: cst.Module) -> Iterator[TypeImport]:
        """The imports that supplied the annotations this pass replaced, as they were written."""
        if not self.claims.replaced:
            return
        bound = bindings(original.code)
        for annotation in self.claims.replaced:
            text = original.code_for_node(annotation.annotation)
            for name in free(text):
                item = bound.get(name)
                if item is not None:
                    yield item

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
            self.refused.add((name, "VX036"))
            return node
        self._wrote = True
        self._injected = self._injected or any(injection.was != REQUEST for injection in injections)
        added = [
            _param(injection, self.claims)
            for injection in injections
            if injection.asked and injection.was not in _declared(node.params)
        ]
        # A parameter after `*args` is keyword-only, so that is where a new one goes in a signature
        # that has one. So is a signature whose positional-only half carries a default, where a
        # positional parameter without one has nowhere valid to go — velox binds by keyword, so a
        # `*` in front of the new parameter costs the signature nothing it had.
        keyword_only = node.params.star_arg is not cst.MaybeSentinel.DEFAULT or any(
            param.default is not None for param in node.params.posonly_params
        )
        params, reordered = _ordered(
            _rewrite(node.params.params, by_name, () if keyword_only else added, self.claims)
        )
        kwonly = _rewrite(
            node.params.kwonly_params, by_name, added if keyword_only else (), self.claims
        )
        if reordered or (added and not keyword_only):
            params = tuple(param.with_changes(comma=cst.MaybeSentinel.DEFAULT) for param in params)
        star = node.params.star_arg
        if added and keyword_only:
            kwonly = tuple(param.with_changes(comma=cst.MaybeSentinel.DEFAULT) for param in kwonly)
            if star is cst.MaybeSentinel.DEFAULT:
                star = cst.ParamStar()
        return node.with_changes(
            params=node.params.with_changes(params=params, star_arg=star, kwonly_params=kwonly)
        )


def _rewrite(
    params: Sequence[cst.Param],
    by_name: Mapping[str, Injection],
    added: Sequence[cst.Param] = (),
    claims: _Claims | None = None,
) -> tuple[cst.Param, ...]:
    """`params`, each injected one rewritten as its injection, with `added` written on the end.

    An injection the rewrite takes away rather than binds — a `request` with no use left — drops
    out here, and `added` are the parameters a body asked for by name, which the signature never
    declared.
    """
    kept = [param for param in params if not _is_dropped(param, by_name)]
    rewritten = [_inject(param, by_name.get(param.name.value), claims) for param in kept]
    return tuple([*rewritten, *added])


def _ordered(params: Sequence[cst.Param]) -> tuple[tuple[cst.Param, ...], bool]:
    """`params` with the ones carrying no default first, and whether that moved any of them.

    A positional parameter without a default cannot follow one that has it, and only a parameter
    the source wrote carries a default here: an injection is metadata. So this moves exactly one
    thing, a parameter a body asked for by name, ahead of the defaults it was appended after.
    Every other signature keeps the order and the whitespace it was written with.
    """
    defaulted = [param.default is not None for param in params]
    if defaulted == sorted(defaulted):
        return tuple(params), False
    plain = [param for param, has in zip(params, defaulted, strict=True) if not has]
    filled = [param for param, has in zip(params, defaulted, strict=True) if has]
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


def _param(injection: Injection, claims: _Claims | None = None) -> cst.Param:
    """The parameter a dependency asked for by name becomes: injected, and written with a comma."""
    return cst.Param(
        name=cst.Name(injection.param),
        annotation=_annotated(injection, None, claims),
        comma=cst.MaybeSentinel.DEFAULT,
    )


def _inject(
    param: cst.Param, injection: Injection | None, claims: _Claims | None = None
) -> cst.Param:
    if injection is None:
        return param
    if injection.was == REQUEST:
        return param.with_changes(name=cst.Name(PARAM), annotation=None, default=None)
    # An annotation the source already carries is the type half of what is written here: it is the
    # author's answer to the same question, and overwriting it would be this pass deciding a type
    # against someone who had already decided one. A built-in is the exception -- `capsys` becomes
    # a `velox.Capture`, so the `CaptureFixture[str]` the source wrote is no longer true of it.
    ours = injection.retypes or param.annotation is None
    if ours and param.annotation is not None and claims is not None:
        claims.replaced.append(param.annotation)
    carried = None if ours else param.annotation
    return param.with_changes(
        name=cst.Name(injection.param),
        annotation=_annotated(injection, carried, claims),
        default=None,
        equal=cst.MaybeSentinel.DEFAULT,
    )


def _annotated(
    injection: Injection, carried: cst.Annotation | None, claims: _Claims | None
) -> cst.Annotation:
    """`Annotated[T, Depends(fixture)]`, where `T` is `carried` or the type inferred for it."""
    inner = _bare(carried) if carried is not None else _type(injection, claims)
    marker = cst.Call(
        func=cst.Name("Depends"),
        args=[cst.Arg(value=cst.parse_expression(injection.reference))],
    )
    return cst.Annotation(
        annotation=cst.Subscript(
            value=cst.Name(ANNOTATED),
            slice=[
                cst.SubscriptElement(slice=cst.Index(value=inner)),
                cst.SubscriptElement(slice=cst.Index(value=marker)),
            ],
        )
    )


def _bare(annotation: cst.Annotation) -> cst.BaseExpression:
    """The type an annotation states, with an injection it already carries taken off the front.

    An `Annotated[T, Depends(fx)]` is what this pass writes, so a tree it has already converted
    reads back as one: the type is `T`, and rewriting it wraps that rather than the whole
    annotation. Any other annotation is the type it states.
    """
    node = annotation.annotation
    if not isinstance(node, cst.Subscript) or not _named(node.value, ANNOTATED):
        return node
    if len(node.slice) < 2 or not any(_is_marker(element) for element in node.slice[1:]):
        return node
    first = node.slice[0].slice
    return first.value if isinstance(first, cst.Index) else node


def _is_marker(element: cst.SubscriptElement) -> bool:
    """Whether one `Annotated[...]` metadata element is a `Depends()` call."""
    index = element.slice
    if not isinstance(index, cst.Index) or not isinstance(index.value, cst.Call):
        return False
    return _named(index.value.func, "Depends")


def _named(expression: cst.BaseExpression, name: str) -> bool:
    """Whether an expression is `name`, however the module it was written in spells its owner."""
    match expression:
        case cst.Name(value=value):
            return value == name
        case cst.Attribute(attr=cst.Name(value=value)):
            return value == name
        case _:
            return False


def _type(injection: Injection, claims: _Claims | None) -> cst.BaseExpression:
    """The inferred type for this parameter, claiming the imports that make it spellable.

    Claimed here rather than when the plan was made, so that a definition the rewrite backs out of
    leaves no import behind it: nothing is queued until a parameter is actually written with it.
    `Any` is what an injection nothing could be inferred for is written with, which is the type
    such a parameter had anyway, said out loud.
    """
    if injection.annotation is None:
        if claims is not None:
            claims.untyped = True
        return cst.Name(ANY)
    if claims is not None:
        claims.imports.extend(injection.needs)
    return cst.parse_expression(injection.annotation)


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
            return "VX205" if _mentions(node.body, SET_LEVEL) else "VX222"
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

    A fixture an `indirect` mark chose cases for has no `params=` of its own to carry over, so the
    case list the plan took off those marks is written here — with the ids pytest composed from
    it, which is what keeps each test's node id where it was.
    """
    existing = {
        arg.keyword.value: arg
        for arg in (expression.args if isinstance(expression, cst.Call) else ())
        if arg.keyword is not None
    }
    args: list[cst.Arg] = []
    if work.scope != "function":
        args.append(_kwarg("scope", cst.SimpleString(f'"{work.scope}"')))
    written = ("name",) if work.carried is not None else ("name", "params", "ids")
    if work.carried is not None:
        # The cases are the mark's, so anything the decorator said about cases of its own is gone
        # with it — pytest tolerates an `ids=` with no `params=` beside it, and velox does not.
        args.append(_kwarg("params", _values(work.carried.values)))
        args.append(_kwarg("ids", _strings(work.carried.ids)))
    for keyword in written:
        carried = existing.get(keyword)
        if carried is not None:
            args.append(_kwarg(keyword, carried.value))
    return cst.Call(
        func=cst.Attribute(value=cst.Name("velox"), attr=cst.Name("fixture")),
        args=[arg.with_changes(comma=cst.MaybeSentinel.DEFAULT) for arg in args],
    )


def _values(spelled: Sequence[str]) -> cst.List:
    """The case list as one literal, each value written from the `repr` the dump carries."""
    elements = []
    for text in spelled:
        value = parametrize.literal(text)
        assert value is not None, text
        elements.append(cst.Element(value))
    return cst.List(elements)


def _strings(texts: Sequence[str]) -> cst.List:
    return cst.List(
        [cst.Element(cst.SimpleString(json.dumps(text, ensure_ascii=False))) for text in texts]
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


def _type_checking(module: cst.Module, wanted: Sequence[TypeImport]) -> cst.Module:
    """`module` with an `if TYPE_CHECKING:` block holding the imports its annotations are named by.

    Type-checking-only because these imports exist for the annotations and nothing else: the
    module already imports, at run time, every fixture object it injects, and a type one of them
    returns is not a name any line of the converted suite evaluates. Under the future import this
    pass also writes, it never has to be.

    The block goes under the module's imports, and into the one already there if the suite had
    one — which is what makes converting a converted tree add nothing the second time.
    """
    if not wanted:
        return module
    body = list(module.body)
    found = _block(body)
    written = {_rendered(line) for line in body}
    if found is not None:
        written |= {_rendered(line) for line in found[2].body}
    lines = [
        line
        for line in (cst.parse_statement(text) for text in statements(wanted))
        if _rendered(line) not in written
    ]
    if not lines:
        return module
    if found is not None:
        index, block, suite = found
        body[index] = block.with_changes(body=suite.with_changes(body=[*suite.body, *lines]))
        return module.with_changes(body=body)
    guard = cst.If(
        test=cst.Name("TYPE_CHECKING"),
        body=cst.IndentedBlock(body=lines),
        leading_lines=[cst.EmptyLine()],
    )
    body.insert(_after_imports(body), guard)
    return module.with_changes(body=body)


def _block(body: Sequence[cst.BaseStatement]) -> tuple[int, cst.If, cst.IndentedBlock] | None:
    """The `if TYPE_CHECKING:` the module already writes at its top level, and where it is."""
    for index, statement in enumerate(body):
        if not isinstance(statement, cst.If) or not isinstance(statement.body, cst.IndentedBlock):
            continue
        match statement.test:
            case cst.Name(value="TYPE_CHECKING") | cst.Attribute(attr=cst.Name("TYPE_CHECKING")):
                return index, statement, statement.body
            case _:
                continue
    return None


def _after_imports(body: Sequence[cst.BaseStatement]) -> int:
    """The first index past the module's opening run of imports, which is where the block goes."""
    last = 0
    for index, statement in enumerate(body):
        if not isinstance(statement, cst.SimpleStatementLine):
            continue
        if all(isinstance(part, cst.Import | cst.ImportFrom) for part in statement.body):
            last = index + 1
        elif last:
            break
    return last


def _rendered(statement: cst.CSTNode) -> str:
    """One statement as the text it writes, for comparing it against one already in the module."""
    return cst.Module(body=[]).code_for_node(statement).strip()
