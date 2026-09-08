"""Writing the type: which annotation each injection gets, and what import makes it spellable.

`voci_migrate.inference` answers what the type *is*, from the factory's return annotation alone.
This is the other half — whether the names in that answer can be spelled in the module the
parameter is written in, and what import puts them there — plus the built-in table, whose types
no suite states and inference therefore cannot reach. `convert/wiring.py` emits both halves,
inside the `Annotated[...]` that carries the injection.

Where the type is not recoverable, the parameter is written `Any` and the fixture behind it gets a
row in the conversion report. An injected parameter has no default for a checker to infer from, so
`Any` written down is what the parameter means either way, said out loud.

Named `annotate` rather than `annotations` because `convert/__init__.py` writes `from __future__
import annotations`, which binds that name in the package's own namespace: a submodule called
`annotations` is shadowed there until something imports it, and a checker never sees it at all.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from voci_migrate.convert import layout
from voci_migrate.convert.layout import CONFTEST, Layout
from voci_migrate.inference import (
    TypeImport,
    bindings,
    factory,
    for_factory,
    free,
    infer,
    rebind,
)
from voci_migrate.model import FixtureDef

__all__ = [
    "BUILTIN_TYPES",
    "Degraded",
    "Resolver",
    "TypeImport",
    "Typed",
    "bindings",
    "importable",
    "infer",
    "statements",
]


_Factory = ast.FunctionDef | ast.AsyncFunctionDef


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


# The type each voci built-in hands the parameter it is injected into, and the import that names
# it. Written down rather than inferred, because the suite's own sources say nothing about what
# `tmp_path` returns and voci is not a dependency of this tool. Most are spelled through the
# `import voci` a converted module already has, so they need no import of their own;
# `test_a_written_builtin_type_matches_what_voci_declares` pins every row against voci itself,
# which is what keeps the table from drifting.
BUILTIN_TYPES: Mapping[str, tuple[str, TypeImport | None]] = {
    "tmp_path": ("Path", TypeImport("pathlib", "Path")),
    "tmp_path_factory": ("voci.TmpPathFactory", None),
    "tmpdir": ("voci.LegacyPath", None),
    "tmpdir_factory": ("voci.LegacyTmpPathFactory", None),
    "capsys": ("voci.Capture", None),
    "caplog": ("voci.LogRecords", None),
}

# The built-ins whose voci counterpart is the same object pytest's was, so an annotation the
# author already wrote for one is still true of the other and is left exactly as written. Every
# other row above replaces one, because `capsys` really does stop being a `CaptureFixture`.
SAME_AS_PYTEST = frozenset({"tmp_path"})


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
        self._factories: dict[tuple[str, str | None], _Factory | None] = {}
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

    def builtin(self, argname: str, consumer: str) -> Typed | None:
        """The annotation an injection of pytest's `argname` gets from voci's counterpart.

        No `Degraded` row when there is none: a built-in this does not name is one voci has no
        counterpart for, so there is no injection to lose a type at, and nothing for a user to go
        and annotate either.
        """
        found = BUILTIN_TYPES.get(argname)
        if found is None:
            return None
        annotation, origin = found
        if origin is None:
            return Typed(annotation=annotation, imports=())
        spelling = self._decide(origin.bound, hint="voci", consumer=consumer, origin=origin)
        return Typed(
            annotation=rebind(annotation, origin.bound, spelling.bound),
            imports=() if spelling.item is None else (spelling.item,),
        )

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
        node = self._factory(source, text, fixture.func.qualname)
        if node is None:
            return None, "factory not found in its own source"
        annotation = for_factory(fixture.returns, node, text, source)
        if annotation is None:
            return None, f"nothing to infer from `-> {fixture.returns}`"
        wanted: list[TypeImport] = []
        for name in free(annotation):
            spelling = self._spell(name, source=source, consumer=consumer)
            if spelling is None:
                return None, f"`{name}` is not attributable to an importable module"
            if spelling.bound != name:
                annotation = rebind(annotation, name, spelling.bound)
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
        hint = PurePosixPath(source).parent.name or "root"
        return self._decide(name, hint=hint, consumer=consumer, origin=origin)

    def _decide(self, name: str, *, hint: str, consumer: str, origin: TypeImport) -> _Spelling:
        """How `consumer` spells `origin`, deciding once and answering the same way after.

        Two annotations wanting the same name in one module are one import, and the second must
        not read the name the first has just claimed as a collision with something else.
        """
        decided = self._spelled.get((consumer, origin))
        if decided is not None:
            return decided
        spelling = self._first(name, hint=hint, consumer=consumer, origin=origin)
        self._spelled[(consumer, origin)] = spelling
        return spelling

    def _first(self, name: str, *, hint: str, consumer: str, origin: TypeImport) -> _Spelling:
        taken = self._claimed(consumer)
        if name not in taken:
            taken.add(name)
            return _Spelling(bound=name, item=origin)
        if self._table(self._origin.get(consumer, consumer)).get(name) == origin:
            # The consumer already imports that symbol out of that module, so the name it binds is
            # the one this annotation means and there is no second import to write.
            return _Spelling(bound=name, item=None)
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

    def _factory(self, source: str, text: str, qualname: str | None) -> _Factory | None:
        """`factory(text, qualname)`, parsed once per `(source, qualname)` rather than per site.

        A fixture injected into hundreds of sites would otherwise have its defining module
        reparsed and walked once per site; every one of them wants the same node.
        """
        key = (source, qualname)
        if key not in self._factories:
            self._factories[key] = factory(text, qualname)
        return self._factories[key]

    def _table(self, file: str) -> Mapping[str, TypeImport | None]:
        table = self._bindings.get(file)
        if table is None:
            text = self._sources.get(file)
            table = bindings(text, file) if text is not None else {}
            self._bindings[file] = table
        return table


def _defined_at(fixture: FixtureDef) -> str:
    """Where a fixture's factory is written, as the `file:lineno` a worklist is read as."""
    file = fixture.func.file or layout.owning_file(fixture) or "?"
    return f"{file}:{fixture.func.lineno}" if fixture.func.lineno else file
