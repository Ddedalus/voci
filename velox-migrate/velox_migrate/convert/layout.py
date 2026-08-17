"""Where every translated fixture ends up, and how the tests that use it name it.

velox wires dependencies by importing the fixture object, so the file layout is the wiring: a
fixture nobody can import is a fixture nobody can use. The layout preserves the geography pytest
gave the suite — each directory that had a `conftest.py` gets a `fixtures.py` beside it — because
that keeps every fixture's move as short as possible and leaves the diff readable.

Two questions this settles that pytest never had to ask. A fixture's binding name is the name its
factory is written under, which is not always the name tests requested it by; and two directories
may bind the same name, which pytest kept apart by directory and an import statement cannot, so
the import aliases by directory.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from velox_migrate.model import FixtureDef

FIXTURES_MODULE = "fixtures.py"
CONFTEST = "conftest.py"


@dataclass(frozen=True, slots=True)
class Home:
    """The module a fixture object lives in once translated, and the name it is bound to there."""

    key: str
    argname: str
    symbol: str
    source: str
    module: str

    @property
    def moved(self) -> bool:
        return self.module != self.source

    @property
    def dotted(self) -> str:
        return dotted(self.module)


@dataclass(frozen=True, slots=True)
class Import:
    """One `from ... import ...` a consuming module needs, and the name it binds."""

    module: str
    symbol: str
    alias: str | None = None

    @property
    def bound(self) -> str:
        return self.alias or self.symbol

    def __str__(self) -> str:
        tail = f"{self.symbol} as {self.alias}" if self.alias else self.symbol
        return f"from {self.module} import {tail}"


@dataclass(frozen=True, slots=True)
class Layout:
    """The placement decisions for one conversion.

    `moves` maps each `conftest.py` onto the `fixtures.py` its content becomes; `imports` is keyed
    by the consuming file and the fixture it consumes, since the same fixture is imported under
    different names by two modules that already bind its own.
    """

    homes: Mapping[str, Home]
    moves: Mapping[str, str]
    imports: Mapping[tuple[str, str], Import]

    def home(self, key: str) -> Home | None:
        return self.homes.get(key)

    def importing(self, consumer: str, key: str) -> Import | None:
        """The import `consumer` needs to name fixture `key`, or `None` if it already can."""
        return self.imports.get((consumer, key))

    def imports_for(self, consumer: str) -> tuple[Import, ...]:
        """Every import `consumer` needs, deduplicated and in a stable order."""
        found = {
            (item.module, item.symbol, item.alias)
            for (file, _), item in self.imports.items()
            if file == consumer
        }
        return tuple(Import(*entry) for entry in sorted(found))

    @property
    def modules(self) -> tuple[str, ...]:
        """Every module a translated fixture ends up in, sorted."""
        return tuple(sorted({home.module for home in self.homes.values()}))


def owning_file(fixture: FixtureDef) -> str | None:
    """The suite file a fixture was written in, read from where pytest made it visible.

    A fixture's recorded factory location is the wrapper's when a decorator hid the function, so
    visibility is the honest answer: a directory node means the `conftest.py` in it, and a module
    or class node names the module itself. The rootdir is spelled `"."`; the empty node belongs to
    a globally registered fixture, which has no file in the suite at all, and that is what tells a
    plugin's fixture from the suite's own.
    """
    node = fixture.visibility
    if node == "":
        return None
    if node == ".":
        return CONFTEST
    file = node.partition("::")[0]
    if file.endswith(".py"):
        return file
    return str(PurePosixPath(node) / CONFTEST)


def home_module(source: str) -> str:
    """The module a fixture written in `source` ends up in."""
    path = PurePosixPath(source)
    return str(path.with_name(FIXTURES_MODULE)) if path.name == CONFTEST else source


def dotted(module: str) -> str:
    """`tests/integration/fixtures.py` as the absolute import path `tests.integration.fixtures`.

    Always absolute and rootdir-relative: velox imports test modules under synthetic
    `velox_tests.*` names, so a relative import between two of them resolves against nothing.
    """
    path = PurePosixPath(module)
    return ".".join([*path.parent.parts, path.stem]) if path.parent.parts else path.stem


def plan(
    fixtures: Mapping[str, FixtureDef],
    *,
    symbols: Mapping[tuple[str, str], str],
    consumers: Mapping[str, Iterable[str]],
    source_of: Callable[[str], str | None],
) -> Layout:
    """Place `fixtures`, given the symbol each is bound to and who imports it.

    `symbols` maps an owning file and a fixture's argname onto the name its factory is written
    under, which only the source can say. `consumers` maps each file onto the fixture keys the
    code in it names. `source_of` reads a file's text, for the names a module already binds.
    """
    homes: dict[str, Home] = {}
    moves: dict[str, str] = {}
    for key, fixture in sorted(fixtures.items()):
        source = owning_file(fixture)
        symbol = symbols.get((source, fixture.argname)) if source is not None else None
        if source is None or symbol is None:
            continue
        module = home_module(source)
        homes[key] = Home(
            key=key, argname=fixture.argname, symbol=symbol, source=source, module=module
        )
        if module != source:
            moves[source] = module

    bound = _bound_names({*consumers, *(home.module for home in homes.values())}, moves, source_of)
    return Layout(homes=homes, moves=moves, imports=_imports(homes, consumers, bound))


def module_level_names(source: str) -> frozenset[str]:
    """Every name a module binds at its top level, as far as a parse can see.

    Imports, assignments, functions and classes — the bindings an added import could collide with.
    Unparseable source binds nothing, which errs toward aliasing an import that needed no alias.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return frozenset()
    found: set[str] = set()
    for node in tree.body:
        match node:
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
                found.add(node.name)
            case ast.Import() | ast.ImportFrom():
                for alias in node.names:
                    found.add(alias.asname or alias.name.partition(".")[0])
            case ast.Assign():
                found |= {target.id for target in node.targets if isinstance(target, ast.Name)}
            case ast.AnnAssign(target=ast.Name(id=name)):
                found.add(name)
            case _:
                pass
    return frozenset(found)


def _bound_names(
    files: Iterable[str],
    moves: Mapping[str, str],
    source_of: Callable[[str], str | None],
) -> dict[str, frozenset[str]]:
    """The top-level names of each of `files`, keyed by the file as it ends up.

    Every consuming module is here, not only the fixture modules: a test module that already binds
    `payload` — a helper, a constant — is exactly where an import of a fixture called `payload`
    has to be aliased, and it is the module a collision is silent in, since the later binding
    simply wins.

    A `fixtures.py` binds whatever the `conftest.py` it came from bound, since the conftest's
    content travels with its fixtures.
    """
    origin = {target: source for source, target in moves.items()}
    bound: dict[str, frozenset[str]] = {}
    for file in files:
        text = source_of(origin.get(file, file))
        bound[file] = module_level_names(text) if text is not None else frozenset()
    return bound


def _imports(
    homes: Mapping[str, Home],
    consumers: Mapping[str, Iterable[str]],
    bound: Mapping[str, frozenset[str]],
) -> dict[tuple[str, str], Import]:
    imports: dict[tuple[str, str], Import] = {}
    for consumer, keys in sorted(consumers.items()):
        taken = set(bound.get(consumer, frozenset()))
        for key in sorted(keys):
            home = homes.get(key)
            if home is None or home.module == consumer:
                continue
            symbol = home.symbol
            alias = None if symbol not in taken else _alias(home, taken)
            taken.add(alias or symbol)
            imports[(consumer, key)] = Import(home.dotted, symbol, alias)
    return imports


def _alias(home: Home, taken: set[str]) -> str:
    """A name for `home`'s symbol that `taken` leaves free, keyed by where the fixture lives.

    Two directories may each bind `client`, and pytest kept them apart by directory, so the
    directory is what the alias reintroduces.
    """
    parent = PurePosixPath(home.module).parent.name or "root"
    candidate = f"{parent}_{home.symbol}"
    suffix = 2
    while candidate in taken:
        candidate = f"{parent}_{home.symbol}_{suffix}"
        suffix += 1
    return candidate
