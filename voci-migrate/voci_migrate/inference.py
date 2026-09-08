"""What an injected parameter's type is, read off the fixture factory's return annotation.

Not every checker derives that type from `Depends(fx)` — mypy gives the parameter `Any` and says
nothing — so the conversion writes it down, and the audit says which fixtures leave it nothing to
write. Those are the same inspection asked from two sides, and this is the inspection. Neither
side is about converting, which is why the rules sit above `convert/` rather than inside it:
`audit/readiness.py` cannot import that package without a cycle.

Everything here works on the annotation's **source text** and never evaluates it. Text is the one
form that means the same thing under every annotation regime — objects in a plain module, strings
under `from __future__ import annotations`, lazy and raising from 3.14 — and it is what
`model.FixtureDef.returns` carries out of the extractor.
"""

from __future__ import annotations

import ast
import builtins
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

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


def rebind(annotation: str, name: str, replacement: str) -> str:
    """`annotation` with every use of `name` in it spelled `replacement` instead."""
    tree = ast.parse(annotation, mode="eval")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            node.id = replacement
    return ast.unparse(tree.body)


def for_factory(
    returns: str | None,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    source: str,
    file: str = "",
) -> str | None:
    """`infer`, given every argument the factory's own definition and module supply.

    The one entry point for asking a real fixture what its parameters are typed as, so that the
    audit's worklist and the conversion's report cannot answer it differently — which they did
    while one of them passed `imports` and the other did not.
    """
    return infer(
        returns,
        is_async=isinstance(node, ast.AsyncFunctionDef),
        generator=yields(node),
        imports=bindings(source, file),
    )


def factory(source: str, qualname: str | None) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
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


def yields(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
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
