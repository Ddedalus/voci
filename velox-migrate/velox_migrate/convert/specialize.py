"""Override chains, written out as the objects a directory's tests import instead.

pytest let one name mean two things depending on where the test asking for it lived, and resolved
which at run time. velox has one object per fixture, so the two meanings become two objects — and
every fixture written between the override and a test that reaches it has to become two as well,
since each of the pair is wired to a different definition of the name below it.

That is what a specialized chain is: for each directory that redefines a fixture, a copy of every
fixture it sits above, placed beside the override and named for the directory, with the copies
wired to each other and to the override. The originals stay exactly as they are, for the tests
outside that directory that still resolve them.

A copy is the source of its original, re-bound under a new name — nothing in the body is read or
rewritten here. The `@pytest.fixture` decorator, the signature and the imports the body needs
travel with it, and the ordinary conversion of the module it lands in translates all three.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

import libcst as cst

from velox_migrate.audit.wiring import Override, under
from velox_migrate.convert import layout
from velox_migrate.convert.layout import Import
from velox_migrate.model import FixtureDef, GroundTruth

__all__ = ["NONE", "Copy", "Specialization", "plan"]


@dataclass(frozen=True, slots=True)
class Copy:
    """One fixture duplicated for the subtree an override rules.

    `key` is this copy's own key in the fixture graph, distinct from `origin`'s so that a consumer
    inside the subtree and one outside it name different objects. `module` is where the copy is
    written and `host` the file whose rewrite writes it there, which differ for a `conftest.py`.
    `code` is the copy's source, already re-bound to `symbol`, in the pytest spelling the original
    was written in.
    """

    key: str
    origin: str
    node: str
    symbol: str
    module: str
    host: str
    scope: str
    parametrized: bool
    code: str
    imports: tuple[Import, ...]
    needs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Specialization:
    """Every copy this conversion writes, and who names which.

    `copies` is keyed by each copy's own key, and `by_node` maps a fixture and an overriding node
    onto the copy written for it. `deepest` orders the overriding nodes innermost first, which is
    what makes nested overrides resolve the way pytest resolved them: a consumer under two of them
    takes the copy from the nearer.
    """

    copies: Mapping[str, Copy]
    deepest: tuple[str, ...]
    by_node: Mapping[tuple[str, str], str]

    def redirect(self, consumer: str, key: str) -> str:
        """What `consumer` gets asking for `key`: a copy where it is inside one, else `key`."""
        for node in self.deepest:
            if under(node, consumer):
                found = self.by_node.get((key, node))
                if found is not None:
                    return found
        return key

    @property
    def homes(self) -> Mapping[str, layout.Home]:
        """Where each copy lives, in the shape the layout places every other fixture in."""
        return {
            copy.key: layout.Home(
                key=copy.key,
                argname=copy.symbol,
                symbol=copy.symbol,
                source=copy.module,
                module=copy.module,
            )
            for copy in self.copies.values()
        }


#: The specialization of a suite with no override the conversion writes a chain for.
NONE = Specialization(copies={}, deepest=(), by_node={})


def plan(
    ground_truth: GroundTruth,
    overrides: Sequence[Override],
    *,
    converting: Mapping[str, FixtureDef],
    symbols: Mapping[tuple[str, str], str],
    source_of: Callable[[str], str | None],
    taken: Mapping[str, frozenset[str]],
) -> Specialization:
    """Plan the copies `overrides` needs, for the fixtures `converting` is translating.

    An override the conversion is leaving alone is skipped, and so is one whose chain reaches a
    fixture that is: every test that would have read the missing copy is refused already, since it
    reaches a fixture nothing translated.
    """
    copies: dict[str, Copy] = {}
    by_node: dict[tuple[str, str], str] = {}
    claimed: dict[str, set[str]] = {}

    for override in overrides:
        node = override.node
        host = layout.owning_file(override.winner)
        if override.winner.key not in converting or host is None:
            continue
        module = layout.home_module(host)
        if not all(key in converting for key in override.downstream):
            continue
        # Names are claimed against a copy of what the module binds, so an override that turns out
        # to be unspecializable leaves no name reserved behind it.
        destination = source_of(host) or ""
        taken_here = set(claimed.setdefault(module, set(taken.get(module, frozenset()))))
        planned: list[Copy] = []
        for key in sorted(override.downstream):
            if (key, node) in by_node:
                continue
            copy = _copy(
                ground_truth.fixture_defs[key],
                node=node,
                module=module,
                host=host,
                symbols=symbols,
                source_of=source_of,
                taken=taken_here,
                destination=destination,
            )
            if copy is None:
                # One of the chain has no source to copy, or copying it would change what a name
                # already means where it lands — and half a chain wires the subtree to the
                # definition the override exists to replace.
                planned = []
                break
            planned.append(copy)
        for copy in planned:
            copies[copy.key] = copy
            by_node[(copy.origin, node)] = copy.key
            claimed[module].add(copy.symbol)

    return Specialization(
        copies=copies,
        deepest=tuple(sorted({node for _, node in by_node}, key=lambda n: (-len(n), n))),
        by_node=by_node,
    )


def _copy(
    origin: FixtureDef,
    *,
    node: str,
    module: str,
    host: str,
    symbols: Mapping[tuple[str, str], str],
    source_of: Callable[[str], str | None],
    taken: set[str],
    destination: str,
) -> Copy | None:
    source, container = layout.owning_file(origin), layout.owning_container(origin)
    symbol = symbols.get((container, origin.argname)) if container is not None else None
    text = source_of(source) if source is not None else None
    if source is None or symbol is None or text is None:
        return None
    name = _free_name(f"{origin.argname}_{_suffix(node)}", taken)
    written = _rebind(text, symbol, name)
    if written is None:
        return None
    carried, needs = _carried(text, symbol, home=layout.dotted(layout.home_module(source)))
    if _clashes(destination, carried, needs):
        return None
    taken.add(name)
    return Copy(
        key=f"{origin.key}#{node}",
        origin=origin.key,
        node=node,
        symbol=name,
        module=module,
        host=host,
        scope=origin.scope,
        parametrized=origin.is_parametrized,
        code=written,
        imports=carried,
        needs=needs,
    )


def _suffix(node: str) -> str:
    """What a copy is named for: the class or directory the override rules, else its module."""
    file, _, holder = node.partition("::")
    name = layout.snake(holder) if holder else PurePosixPath(file).name
    cleaned = "".join(character if character.isalnum() else "_" for character in name)
    return cleaned.removesuffix("_py").strip("_") or "specialized"


def _free_name(candidate: str, taken: set[str]) -> str:
    name, suffix = candidate, 2
    while name in taken:
        name = f"{candidate}_{suffix}"
        suffix += 1
    return name


def _rebind(text: str, symbol: str, name: str) -> str | None:
    """The source of the module-level `def symbol` in `text`, written under `name`.

    Returns `None` where the file holds no such definition, which is the same answer the layout
    gives a fixture whose factory it cannot find: the caller declines to copy it.
    """
    try:
        module = cst.parse_module(text)
    except cst.ParserSyntaxError:
        return None
    for statement in module.body:
        if isinstance(statement, cst.FunctionDef) and statement.name.value == symbol:
            # Blank lines and comments above the original belong to where it was written, not to
            # a copy appended somewhere else.
            rebound = statement.with_changes(name=cst.Name(name), leading_lines=[])
            return cst.Module(body=[rebound]).code
    return None


def _carried(text: str, symbol: str, *, home: str) -> tuple[tuple[Import, ...], tuple[str, ...]]:
    """What a copy of `symbol` has to import to read the same names its original read.

    A name the module imported is imported the same way again, so the copy resolves it exactly as
    the original did — plainly where the module imported it plainly, which is what keeps the copy
    off a `pytest` name the rewrite is about to take away. Anything else the module binds — a
    helper, a constant, a decorator written there — is imported from where that module ends up.
    """
    definition = _definition(text, symbol)
    if definition is None:
        return (), ()
    wanted = _free_names(definition)
    imports: list[Import] = []
    needs: list[str] = []
    for statement, bound in _bindings(text):
        if bound not in wanted:
            continue
        match statement:
            case ast.Import(names=names):
                alias = next(a for a in names if (a.asname or a.name.partition(".")[0]) == bound)
                if alias.asname is None:
                    needs.append(alias.name)
                else:
                    imports.append(Import(home, bound))
            case ast.ImportFrom(module=str(source), level=0, names=names):
                alias = next(a for a in names if (a.asname or a.name) == bound)
                imports.append(Import(source, alias.name, alias.asname))
            case _:
                imports.append(Import(home, bound))
    return tuple(imports), tuple(needs)


def _clashes(destination: str, carried: Sequence[Import], needs: Sequence[str]) -> bool:
    """Whether `destination` already binds one of the names a copy carries, and binds it to else.

    The imports a copy needs are written into a module that has its own, so a `stamp` there
    already meaning something is not a name this can take: rebinding it would change the module's
    own fixtures as well as the copy.
    """
    bound = {name: _binds(statement, name) for statement, name in _bindings(destination)}
    wanted = {item.bound: (item.module, item.symbol) for item in carried}
    wanted |= {module.partition(".")[0]: (module, None) for module in needs}
    return any(name in bound and bound[name] != source for name, source in wanted.items())


def _binds(statement: ast.stmt, name: str) -> tuple[str, str | None] | None:
    """What `statement` binds `name` to, in the shape a carried import is written as."""
    match statement:
        case ast.ImportFrom(module=str(source), level=0, names=names):
            alias = next(a for a in names if (a.asname or a.name) == name)
            return (source, alias.name)
        case ast.Import(names=names):
            alias = next(a for a in names if (a.asname or a.name.partition(".")[0]) == name)
            return (alias.name, None) if alias.asname is None else (alias.name, alias.asname)
        case _:
            return None


def _definition(text: str, symbol: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == symbol:
            return node
    return None


def _bindings(text: str) -> Iterator[tuple[ast.stmt, str]]:
    """Every module-level statement of `text` against each name it binds."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return
    yield from _bound_in(tree.body)


def _bound_in(body: Sequence[ast.stmt]) -> Iterator[tuple[ast.stmt, str]]:
    """The bindings in `body`, reaching into the blocks a module puts imports inside.

    An import written under `try:` or `if TYPE_CHECKING:` binds its name at module level like any
    other, and a copy whose body reads that name needs it carried over just the same.
    """
    for node in body:
        match node:
            case ast.Import() | ast.ImportFrom():
                for alias in node.names:
                    yield node, alias.asname or alias.name.partition(".")[0]
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
                yield node, node.name
            case ast.Assign(targets=targets):
                for target in targets:
                    if isinstance(target, ast.Name):
                        yield node, target.id
            case ast.AnnAssign(target=ast.Name(id=name)):
                yield node, name
            case ast.If() | ast.Try() | ast.With() | ast.For() | ast.While():
                for nested in _blocks(node):
                    yield from _bound_in(nested)
            case _:
                pass


def _blocks(node: ast.stmt) -> Iterator[Sequence[ast.stmt]]:
    """Every statement list a module-level compound statement holds."""
    for attribute in ("body", "orelse", "finalbody"):
        found = getattr(node, attribute, None)
        if found:
            yield found
    for handler in getattr(node, "handlers", ()):
        yield handler.body


def _free_names(definition: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    """Every name this definition reads and does not bind itself.

    Its parameters are what the wiring swap answers, so they are not free; everything else it
    reads has to be in scope wherever the copy is written.
    """
    bound = {argument.arg for argument in _arguments(definition)}
    read: set[str] = set()
    for node in ast.walk(definition):
        match node:
            case ast.Name(ctx=ast.Store(), id=name):
                bound.add(name)
            case ast.Name(id=name):
                read.add(name)
            case (
                ast.FunctionDef(name=name)
                | ast.AsyncFunctionDef(name=name)
                | ast.ClassDef(name=name)
            ) if node is not definition:
                bound.add(name)
            case _:
                pass
    return frozenset(read - bound)


def _arguments(definition: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.arg]:
    arguments = definition.args
    yield from arguments.posonlyargs
    yield from arguments.args
    yield from arguments.kwonlyargs
    for optional in (arguments.vararg, arguments.kwarg):
        if optional is not None:
            yield optional
