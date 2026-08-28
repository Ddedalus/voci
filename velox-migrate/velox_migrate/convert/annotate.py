"""The type an injected parameter is given, inferred from the fixture factory's return annotation.

An injected parameter's type is not something every checker reads off `Depends(fx)` — mypy gives
it `Any` and says nothing — so the conversion writes it: `db: Session = Depends(db_fx)` rather
than `db=Depends(db_fx)`. What the type *is* follows by inspection from the factory's return
annotation, and this module is the inspection. It works on the annotation's source text and never
evaluates it, because the text is the only form that means the same thing under every annotation
regime.

Two halves, deliberately kept apart. `infer` is the rule table: annotation text in, parameter type
text out, mirroring `FixtureDecorator`'s own overloads so that the type written always agrees with
what a checker already derives for the `Depends()` expression beside it. `Resolver` is everything
else — whether the names in that text can be spelled in the module the parameter is written in,
and what import makes them so. `convert/wiring.py` emits both. When `Depends` moves inside
`Annotated[...]`, the emission changes and neither half here does.

**Where the type is not recoverable this writes no annotation at all**, where
`plans/dependency-typing-plan.md` says to write `Any`. The plan's `Any` is premised on the
annotated spelling, in which a parameter has no default for a checker to infer from; in default
position there is one, and pyright and pyrefly both infer `Session` from `Depends(db_fx)` on their
own. `db: Any = Depends(db_fx)` would destroy that inference and buy mypy nothing, since mypy
already reads the parameter as `Any` either way. So a fallback is silence in the source and a row
in the conversion report — do not restore the plan's letter without moving to `Annotated` first.

Named `annotate` rather than `annotations` because `convert/__init__.py` writes `from __future__
import annotations`, which binds that name in the package's own namespace: a submodule called
`annotations` is shadowed there until something imports it, and a checker never sees it at all.
"""

from __future__ import annotations

import ast
import builtins
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from velox_migrate.convert import layout
from velox_migrate.convert.layout import CONFTEST, Layout
from velox_migrate.model import FixtureDef

# What `FixtureDecorator`'s overloads unwrap, and nothing else. `Generator` and `AsyncGenerator`
# are here because they are subtypes of the `Iterator` and `AsyncIterator` those overloads name;
# `Iterable` is not here because it is not one of them, and a factory annotated `-> Iterable[X]`
# really does hand its parameter an `Iterable[X]`.
_ASYNC_ITERATOR = frozenset({"AsyncIterator", "AsyncGenerator"})
_ITERATOR = frozenset({"Iterator", "Generator"})
_AWAITABLE = frozenset({"Awaitable", "Coroutine"})

# The modules those names are spelled out of, for the dotted forms. An owner outside this set is
# left alone rather than unwrapped by its last component: a `mymod.Generator` is somebody else's
# class.
_TYPING = frozenset({"typing", "typing_extensions", "collections.abc", "abc"})

# Annotations that say nothing a parameter could be checked against: `Any` itself, and a bare
# wrapper, whose overload unwraps to `Any` too.
_OPAQUE = frozenset({"Any"}) | _ASYNC_ITERATOR | _ITERATOR | _AWAITABLE

_BUILTINS = frozenset(dir(builtins))


@dataclass(frozen=True, slots=True)
class TypeImport:
    """One import a written annotation needs, for the consuming module's `TYPE_CHECKING` block.

    `symbol` is `None` for a plain `import module`, which is what a dotted annotation such as
    `os.PathLike` needs; every other form is a `from module import symbol`.
    """

    module: str
    symbol: str | None = None
    alias: str | None = None

    @property
    def bound(self) -> str:
        """The name this import puts in the consuming module's namespace."""
        if self.alias is not None:
            return self.alias
        if self.symbol is not None:
            return self.symbol
        return self.module.partition(".")[0]

    def __str__(self) -> str:
        if self.symbol is None:
            tail = f"{self.module} as {self.alias}" if self.alias else self.module
            return f"import {tail}"
        tail = f"{self.symbol} as {self.alias}" if self.alias else self.symbol
        return f"from {self.module} import {tail}"


@dataclass(frozen=True, slots=True)
class Degraded:
    """One injection that got no type, named the way a user's worklist wants it.

    `fixture` and `defined` name the factory to go and annotate; `site` is the definition that
    lost the type, and how many of those there are per fixture is what orders the worklist.
    """

    fixture: str
    defined: str
    site: str
    reason: str

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.fixture, self.defined, self.site)


@dataclass(frozen=True, slots=True)
class Typed:
    """The annotation one injection is written with, and the imports that make it spellable."""

    annotation: str
    imports: tuple[TypeImport, ...] = ()


@dataclass(frozen=True, slots=True)
class _Spelling:
    """How a consuming module spells one name: what it binds it to, and the import that binds it.

    `item` is `None` for a name the module can already spell, which is the answer for an
    annotation written in the module that defines its own types.
    """

    bound: str
    item: TypeImport | None


def infer(
    returns: str | None,
    *,
    is_async: bool = False,
    generator: bool = False,
    imports: Mapping[str, TypeImport | None] | None = None,
) -> str | None:
    """The type an injected parameter gets from a factory annotated `-> returns`, or `None`.

    `imports` is what the factory's own module binds, which is how a qualified spelling is read:
    `import typing as t` makes `t.Iterator[X]` one of the shapes to unwrap, and without the map
    there is no way to tell it from somebody else's `t`.

    Mirrors `FixtureDecorator`'s overload order — async iterator, iterator, awaitable, plain — on
    the callable's *return type*, which for a coroutine function is the `Coroutine[..., returns]`
    whose last argument is all the annotation names. That is why `is_async` and `generator` are
    asked for: an `async def` that yields is an async generator whose return type is the
    annotation as written, and one that returns is a coroutine wrapping it.

    Unwrapping goes by the annotation's shape and not by what the body does, exactly as the
    overloads resolve: a non-generator factory annotated `-> Iterator[X]` hands its parameter an
    iterator at run time, and every checker still reads `Depends(fx)` as `X`. Writing `X` keeps
    the annotation and the expression beside it agreeing, which is the property that matters —
    the disagreement with the run-time value is `FixtureDecorator`'s, and is documented there.
    """
    if returns is None:
        return None
    try:
        node = ast.parse(returns, mode="eval").body
    except (SyntaxError, ValueError):
        return None
    # A coroutine function's return type is `Coroutine[Any, Any, returns]`, which the
    # `Awaitable[T]` overload unwraps to `returns` — once, so `async def f() -> Iterator[X]` is an
    # `Iterator[X]` and not an `X`.
    unwrapped = node if is_async and not generator else _unwrap(node, imports)
    if _owner(unwrapped, imports) in _OPAQUE or _forward_reference(unwrapped, imports):
        return None
    return ast.unparse(unwrapped)


def roots(annotation: str) -> tuple[str, ...]:
    """Every free name `annotation` needs bound, without repeats.

    `Session | None` needs `Session`, `dict[str, Widget]` needs `Widget`, and `pkg.mod.Thing`
    needs `pkg`: a dotted name is spelled from its leftmost component, so that is the one an
    import has to supply.
    """
    try:
        node = ast.parse(annotation, mode="eval").body
    except (SyntaxError, ValueError):
        return ()
    found = (child.id for child in ast.walk(node) if isinstance(child, ast.Name))
    return tuple(dict.fromkeys(found))


def free(annotation: str) -> tuple[str, ...]:
    """`roots`, less the names every module already has."""
    return tuple(name for name in roots(annotation) if name not in _BUILTINS)


def statements(wanted: Iterable[TypeImport]) -> tuple[str, ...]:
    """`wanted` as the import lines to write: plain imports first, then one line per module.

    Grouped and sorted rather than emitted in the order the annotations happened to want them, so
    that two conversions of the same suite write the same block.
    """
    plain = sorted({str(item) for item in wanted if item.symbol is None})
    by_module: dict[str, set[str]] = {}
    for item in wanted:
        if item.symbol is None:
            continue
        tail = f"{item.symbol} as {item.alias}" if item.alias else item.symbol
        by_module.setdefault(item.module, set()).add(tail)
    grouped = [
        f"from {module} import {', '.join(sorted(names))}"
        for module, names in sorted(by_module.items())
    ]
    return tuple([*plain, *grouped])


def importable(module: str) -> bool:
    """Whether a suite file can be imported from, which a `conftest.py` cannot."""
    return PurePosixPath(module).name != CONFTEST


class Resolver:
    """Decides the annotation each injection is written with, and records what it could not.

    Holds the conversion's sources and its layout, because both questions this answers are about
    where code ends up: the annotation is written in the fixture's module and read in the
    consumer's, and the converter moves the first of those. Stateful in the one way that matters —
    it remembers what it decided per consuming module, so two annotations wanting the same name in
    one module neither both claim it nor disagree about it once one of them has.
    """

    def __init__(self, sources: Mapping[str, str], plan_layout: Layout) -> None:
        self._sources = sources
        self._layout = plan_layout
        self._origin = {target: source for source, target in plan_layout.moves.items()}
        self._bindings: dict[str, Mapping[str, TypeImport | None]] = {}
        self._spelled: dict[tuple[str, TypeImport], _Spelling] = {}
        self._taken: dict[str, set[str]] = {}
        self._degraded: list[Degraded] = []

    @property
    def degraded(self) -> tuple[Degraded, ...]:
        """Every injection that got no type, deduplicated and ordered for the report."""
        return tuple(sorted(set(self._degraded), key=lambda item: item.sort_key))

    def of(self, fixture: FixtureDef, consumer: str, *, site: str) -> Typed | None:
        """The annotation an injection of `fixture` written in `consumer` gets, or `None`.

        Every step falls back rather than guesses: a factory outside the suite, a shape the rules
        do not name, a name no import can attribute to a module, and a consumer already binding
        that name for something else all end the same way — a `Degraded` row, and a parameter
        written exactly as it would have been without any of this.
        """
        typed, reason = self._resolve(fixture, consumer)
        if typed is not None:
            return typed
        self._degraded.append(
            Degraded(
                fixture=fixture.argname, defined=_defined_at(fixture), site=site, reason=reason
            )
        )
        return None

    def _resolve(self, fixture: FixtureDef, consumer: str) -> tuple[Typed | None, str]:
        if fixture.returns is None:
            return None, "no return annotation"
        source = layout.owning_file(fixture)
        text = self._sources.get(source) if source is not None else None
        if source is None or text is None:
            return None, "written outside the suite"
        if fixture.func.file is not None and fixture.func.file != source:
            # A decorator moved the factory to another module, so the names in its annotation are
            # bound in that module's namespace rather than in the one this can read.
            return None, "factory written in another module"
        node = _factory(text, fixture.func.qualname)
        if node is None:
            return None, "factory not found in its own source"
        annotation = infer(
            fixture.returns,
            is_async=isinstance(node, ast.AsyncFunctionDef),
            generator=_yields(node),
            imports=self._table(source),
        )
        if annotation is None:
            return None, f"nothing to infer from `-> {fixture.returns}`"
        wanted: list[TypeImport] = []
        for name in free(annotation):
            spelling = self._spell(name, source=source, consumer=consumer)
            if spelling is None:
                return None, f"`{name}` is not attributable to an importable module"
            if spelling.bound != name:
                annotation = _rebind(annotation, name, spelling.bound)
            if spelling.item is not None:
                wanted.append(spelling.item)
        return Typed(annotation=annotation, imports=tuple(wanted)), ""

    def _spell(self, name: str, *, source: str, consumer: str) -> _Spelling | None:
        """How `consumer` can name what `source` calls `name`, or `None` if it cannot.

        The fixture's own module is where the answer starts, since that is where the annotation
        was written: an import there is one this can repeat, and a class or alias written there
        travels to wherever the converter puts that module's content.
        """
        table = self._table(source)
        if name not in table:
            return None
        home = self._layout.moves.get(source, source)
        origin = table[name]
        if origin is None:
            if not importable(home):
                # Nothing can import a `conftest.py`, so a type written in one the conversion is
                # not moving has no module to be named out of.
                return None
            origin = TypeImport(module=layout.dotted(home), symbol=name)
        if consumer == home:
            return _Spelling(bound=name, item=None)
        # Two injections of the same fixture into one module are one import, and the second must
        # not read the name the first has just claimed as a collision with something else.
        decided = self._spelled.get((consumer, origin))
        if decided is not None:
            return decided
        taken = self._claimed(consumer)
        spelling = self._decide(name, source=source, consumer=consumer, origin=origin, taken=taken)
        self._spelled[(consumer, origin)] = spelling
        return spelling

    def _decide(
        self, name: str, *, source: str, consumer: str, origin: TypeImport, taken: set[str]
    ) -> _Spelling:
        if name not in taken:
            taken.add(name)
            return _Spelling(bound=name, item=origin)
        if self._table(self._origin.get(consumer, consumer)).get(name) == origin:
            # The consumer already imports that symbol out of that module, so the name it binds is
            # the one this annotation means and there is no second import to write.
            return _Spelling(bound=name, item=None)
        hint = PurePosixPath(source).parent.name or "root"
        alias = layout.alias_for(name, hint=hint, taken=taken)
        taken.add(alias)
        # `symbol` stays as it was: `None` is a plain `import mod`, and aliasing it has to write
        # `import mod as alias`, not a `from mod import mod` that names nothing.
        return _Spelling(
            bound=alias,
            item=TypeImport(module=origin.module, symbol=origin.symbol, alias=alias),
        )

    def _claimed(self, consumer: str) -> set[str]:
        """Every name `consumer` binds by the time these annotations are written."""
        claimed = self._taken.get(consumer)
        if claimed is None:
            source = self._origin.get(consumer, consumer)
            text = self._sources.get(source)
            claimed = set(layout.module_level_names(text) if text is not None else ())
            # `module_level_names` stops at the module's own statements, and a name a suite uses
            # only in annotations is exactly the one written inside `if TYPE_CHECKING:`. Missing
            # those would rebind one of them, silently, to something else.
            claimed |= set(self._table(source))
            claimed |= {item.bound for item in self._layout.imports_for(consumer)}
            self._taken[consumer] = claimed
        return claimed

    def _table(self, file: str) -> Mapping[str, TypeImport | None]:
        table = self._bindings.get(file)
        if table is None:
            text = self._sources.get(file)
            table = bindings(text, file) if text is not None else {}
            self._bindings[file] = table
        return table


def bindings(source: str, file: str = "") -> Mapping[str, TypeImport | None]:
    """Every name `source` binds at its top level, against the import that brought it in.

    `None` for a name the module writes itself — a class, a type alias, an assignment — which has
    no import to repeat elsewhere. Imports inside a top-level `if TYPE_CHECKING:` count: that is
    where a name used only in annotations belongs, and an annotation is all this reads.

    `file` is where the module sits under the rootdir, which is what a relative import is relative
    to; without one, a relative import binds its names to nothing.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    found: dict[str, TypeImport | None] = {}
    for node in _top_level(tree):
        match node:
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
                found[node.name] = None
            case ast.Assign():
                found.update({t.id: None for t in node.targets if isinstance(t, ast.Name)})
            case ast.AnnAssign(target=ast.Name(id=name)) | ast.TypeAlias(name=ast.Name(id=name)):
                found[name] = None
            case ast.Import():
                for alias in node.names:
                    bound = alias.asname or alias.name.partition(".")[0]
                    found[bound] = TypeImport(module=alias.name, alias=alias.asname)
            case ast.ImportFrom():
                found.update(_from_import(node, file))
            case _:
                pass
    return found


def _from_import(node: ast.ImportFrom, file: str) -> dict[str, TypeImport | None]:
    """One `from ... import ...`, as the bindings it makes.

    A relative import is spelled absolutely here, rootdir-relative, the way the conversion spells
    every other import it writes — a module a relative import reaches is inside a package, so the
    absolute path to it is one an importing module anywhere in the suite can use. A `..` reaching
    past the rootdir binds nothing, and an annotation needing one of those names falls back.
    """
    module = _absolute(node, file)
    if module is None:
        return {alias.asname or alias.name: None for alias in node.names}
    return {
        alias.asname or alias.name: TypeImport(module, alias.name, alias.asname)
        for alias in node.names
    }


def _absolute(node: ast.ImportFrom, file: str) -> str | None:
    """The module `node` imports from, as an absolute dotted path, or `None` if it has none."""
    if not node.level:
        return node.module or ""
    parts = PurePosixPath(file).parent.parts if file else ()
    if not file or node.level - 1 > len(parts):
        return None
    base = parts[: len(parts) - (node.level - 1)]
    # `from . import x` binds a module rather than a name out of one, and at the rootdir there is
    # no package left to name it from; either way there is no `from ... import ...` to repeat.
    return ".".join([*base, *([node.module] if node.module else [])]) or None


def _top_level(tree: ast.Module) -> Iterator[ast.stmt]:
    """`tree`'s own statements, descending into a top-level `if TYPE_CHECKING:` and nothing else."""
    for node in tree.body:
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            yield from node.body
            continue
        yield node


def _is_type_checking(test: ast.expr) -> bool:
    match test:
        case ast.Name(id="TYPE_CHECKING") | ast.Attribute(attr="TYPE_CHECKING"):
            return True
        case _:
            return False


def _factory(source: str, qualname: str | None) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The `def` `qualname` names in `source`, at the module level or inside one class.

    What it is wanted for is one bit — whether the factory is a generator — which only the body
    can say and which decides how an `async def`'s annotation unwraps.
    """
    if qualname is None or "<locals>" in qualname:
        return None
    parts = qualname.split(".")
    try:
        body: Sequence[ast.stmt] = ast.parse(source).body
    except (SyntaxError, ValueError):
        return None
    for part in parts[:-1]:
        holder = next((n for n in body if isinstance(n, ast.ClassDef) and n.name == part), None)
        if holder is None:
            return None
        body = holder.body
    return next(
        (
            n
            for n in body
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == parts[-1]
        ),
        None,
    )


def _yields(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether this factory is a generator: a `yield` of its own, not one in a function inside it.

    A nested `def` is a closure the factory returns or registers, and its yields are its own.
    """
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        if isinstance(child, ast.Yield | ast.YieldFrom):
            return True
        stack.extend(ast.iter_child_nodes(child))
    return False


def _unwrap(node: ast.expr, imports: Mapping[str, TypeImport | None] | None = None) -> ast.expr:
    """`node` with the one layer `FixtureDecorator` unwraps taken off, if it is one of them."""
    match node:
        case ast.Subscript(value=value, slice=index):
            owner = _owner(value, imports)
            args = list(index.elts) if isinstance(index, ast.Tuple) else [index]
            if owner is None or not args:
                return node
            if owner in _ASYNC_ITERATOR or owner in _ITERATOR:
                return args[0]
            if owner == "Coroutine":
                # `Coroutine[YieldType, SendType, ReturnType]`; only the third is awaited.
                return args[2] if len(args) == 3 else node
            if owner == "Awaitable":
                return args[0]
            return node
        case _:
            return node


def _owner(value: ast.expr, imports: Mapping[str, TypeImport | None] | None = None) -> str | None:
    """The name `value` spells, bare or qualified by a module annotations are written out of."""
    match value:
        case ast.Name(id=name):
            return name
        case ast.Attribute(attr=name):
            prefix = ast.unparse(value).rpartition(".")[0]
            return name if _qualifier(prefix, imports) in _TYPING else None
        case _:
            return None


def _qualifier(prefix: str, imports: Mapping[str, TypeImport | None] | None) -> str:
    """The module `prefix` names where the annotation was written, `prefix` itself if unknown.

    `import typing as t` and `import collections.abc as ca` are the two spellings this exists for:
    the name in front of the dot is the module's own, and only the module's imports say which one
    it is. A name that is not a plain `import` is left as written, since it is not a module.
    """
    head, _, rest = prefix.partition(".")
    item = (imports or {}).get(head)
    if item is None:
        return prefix
    if item.symbol is not None:
        base = f"{item.module}.{item.symbol}" if item.module else item.symbol
    else:
        # Unaliased `import a.b` binds `a`, so the name in front of the dot is the head package.
        base = item.module if item.alias else item.module.partition(".")[0]
    return f"{base}.{rest}" if rest else base


def _forward_reference(
    node: ast.expr, imports: Mapping[str, TypeImport | None] | None = None
) -> bool:
    """Whether `node` holds a stringified name, which is a name this cannot attribute.

    `Literal["a"]`'s strings are values rather than names, and are the one string an annotation
    carries that says nothing about what has to be imported.
    """
    match node:
        case ast.Constant(value=str()):
            return True
        case ast.Subscript(value=value) if _owner(value, imports) == "Literal":
            return _forward_reference(value, imports)
        case _:
            children = (c for c in ast.iter_child_nodes(node) if isinstance(c, ast.expr))
            return any(_forward_reference(child, imports) for child in children)


def _rebind(annotation: str, name: str, replacement: str) -> str:
    """`annotation` with every use of `name` in it spelled `replacement` instead."""
    tree = ast.parse(annotation, mode="eval")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            node.id = replacement
    return ast.unparse(tree.body)


def _defined_at(fixture: FixtureDef) -> str:
    """Where a fixture's factory is written, as the `file:lineno` a worklist is read as."""
    file = fixture.func.file or layout.owning_file(fixture) or "?"
    return f"{file}:{fixture.func.lineno}" if fixture.func.lineno else file
