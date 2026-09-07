"""A loaded dump, as the fixture graph and the tests that resolve through it.

Fixture resolution is per test, not global: the same `engine` fixture reaches a different
`settings` depending on which directory the test that requested it lives in. So the graph's edges
hang off `Item`, which knows the override chain pytest picked for that one test, and the module's
job is to make walking those chains exact — including the case where an override requests the
fixture it overrides.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from velox_migrate import schema
from velox_migrate.schema import DumpError

# pytest lists `request` in a closure like any other name, but it has no definition and nothing
# downstream treats it as a fixture.
REQUEST = "request"


def literal(text: str) -> object:
    """The value a dump's `repr` of a mark argument or a parameter stands for.

    Everything the extractor cannot serialize survives as the text of its `repr`, so a reader that
    wants the value back has to evaluate it, and gets `None` for the ones that were never
    literals — a fixture object, a class, a lambda in a `parametrize` list.
    """
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None


@dataclass(frozen=True, slots=True)
class FuncLocation:
    """Where a fixture's factory is written.

    `wrapped` says whether a decorator was peeled off to get here. A decorator that does not set
    `__wrapped__` cannot be peeled, and then this is the wrapper's own location — in whichever
    module the decorator was written — with `wrapped` false. A fixture's owning conftest is given
    by `FixtureDef.visibility`, which does not depend on this.

    `file` is relative to the suite's rootdir when the file is inside it, and otherwise carries a
    `${site_packages}` or `${prefix}` token for the directory the package was installed into;
    `GroundTruth.resolve_path` expands it.
    """

    module: str | None
    qualname: str | None
    file: str | None
    lineno: int | None
    wrapped: bool


@dataclass(frozen=True, slots=True)
class Mark:
    """One mark applied to a test.

    Arguments are kept as the `repr` of their value, because a mark argument can be any object.
    `origin` is the nodeid of the node the mark was written on — the test itself, its class, or
    its module — and is `None` only in `Item.own_markers`, which records no origin.
    """

    name: str
    args: tuple[str, ...]
    kwargs: Mapping[str, str]
    origin: str | None = None


@dataclass(frozen=True, slots=True)
class FixtureDef:
    """One `@pytest.fixture` definition.

    `visibility` is the nodeid of the directory, module or class the fixture was defined in, and
    empty for a fixture a plugin registered globally; a fixture is visible to a test exactly when
    it prefixes that test's nodeid. `params` and `ids` are `repr`ed values.

    `returns` is the source text of the factory's return annotation, or `None` when it has none.
    It is text and not an object because it is the one thing a dump cannot carry live, and
    because everything downstream unwraps it by inspection rather than by evaluating it. The
    module it is written in is `func.module`, which is what makes a name in it importable.
    """

    key: str
    argname: str
    scope: str
    params: tuple[str, ...] | None
    ids: tuple[str, ...] | str | None
    autouse: bool
    visibility: str
    kind: str
    direct_param: bool
    argnames: tuple[str, ...]
    returns: str | None
    func: FuncLocation

    @property
    def is_parametrized(self) -> bool:
        return self.params is not None


@dataclass(frozen=True, slots=True)
class Dependency:
    """One edge out of a fixture, resolved for the test that is walking it.

    `fixture` is `None` when the dump cannot name a definition — for `request`, and for a name
    that is not visible to this test.
    """

    name: str
    fixture: FixtureDef | None


@dataclass(frozen=True, slots=True)
class CallSpec:
    """The parametrization of a single test case.

    `id` is pytest's own generated id, exactly the text between the brackets of the nodeid.
    A test has a `CallSpec` whether it was parametrized by a mark, indirectly, or by a `params=`
    fixture somewhere in its closure.
    """

    id: str
    idlist: tuple[str, ...]
    params: Mapping[str, str]
    indices: Mapping[str, int]
    marks: tuple[Mark, ...]


@dataclass(frozen=True, slots=True)
class Item:
    """One collected test, with the fixture chain pytest resolved for each name it can see.

    Each chain in `chains` runs furthest-to-closest, so its last element is the definition this
    test actually gets and an override's own super is the element before it.
    """

    nodeid: str
    path: str | None
    lineno: int | None
    originalname: str
    cls: str | None
    argnames: tuple[str, ...]
    initialnames: tuple[str, ...]
    names_closure: tuple[str, ...]
    chains: Mapping[str, tuple[FixtureDef, ...]]
    usefixtures: tuple[str, ...]
    autouse_names: tuple[str, ...]
    own_markers: tuple[Mark, ...]
    markers_with_origin: tuple[Mark, ...]
    callspec: CallSpec | None

    @property
    def is_parametrized(self) -> bool:
        return self.callspec is not None

    def resolve(self, name: str) -> FixtureDef | None:
        """The definition `name` resolves to for this test, nearest-wins already applied."""
        chain = self.chains.get(name)
        return chain[-1] if chain else None

    def dependencies(self, fixture: FixtureDef) -> tuple[Dependency, ...]:
        """What `fixture` requests, resolved as it resolves for this test.

        A fixture that requests its own name is asking for the definition it overrides, so that
        edge is answered from one step down its own chain rather than from the chain's winner —
        which is the fixture itself.
        """
        edges = []
        for name in fixture.argnames:
            if name == fixture.argname:
                edges.append(Dependency(name, self._super_of(fixture)))
            elif name == REQUEST:
                edges.append(Dependency(name, None))
            else:
                edges.append(Dependency(name, self.resolve(name)))
        return tuple(edges)

    def _super_of(self, fixture: FixtureDef) -> FixtureDef | None:
        chain = self.chains.get(fixture.argname, ())
        for position, candidate in enumerate(chain):
            if candidate.key == fixture.key:
                return chain[position - 1] if position else None
        return None

    def walk(self) -> Iterator[FixtureDef]:
        """Every definition this test's fixtures reach, each yielded once.

        Depth-first from the names the test starts with, following each fixture's own requests,
        so a definition that is only reachable as an override's super is included.
        """
        return (fixture for fixture, _ in self.edges())

    def edges(self) -> Iterator[tuple[FixtureDef, tuple[Dependency, ...]]]:
        """`walk()`'s traversal, paired with the dependencies it resolves along the way.

        A caller that wants both a fixture and what it requests would otherwise call
        `dependencies(fixture)` again for every fixture this yields, redoing the same resolution
        `walk()` already did to find where to go next.
        """
        seen: set[str] = set()
        stack = [
            fixture
            for name in reversed(self.initialnames)
            if (fixture := self.resolve(name)) is not None
        ]
        while stack:
            fixture = stack.pop()
            if fixture.key in seen:
                continue
            seen.add(fixture.key)
            deps = self.dependencies(fixture)
            yield fixture, deps
            stack.extend(edge.fixture for edge in reversed(deps) if edge.fixture is not None)


@dataclass(frozen=True, slots=True)
class Plugin:
    name: str
    dist: str
    version: str


@dataclass(frozen=True, slots=True)
class GroundTruth:
    """Everything one extraction run observed about a suite.

    True for the environment it was extracted in and no other: a suite whose fixtures differ by
    platform or plugin version needs one of these per environment, which `environment` identifies.
    """

    extractor_version: int
    pytest_version: str
    environment: Mapping[str, str]
    rootpath: str
    inipath: str | None
    args: tuple[str, ...]
    ini: Mapping[str, str]
    ini_aliases: Mapping[str, str]
    plugins: tuple[Plugin, ...]
    plugin_names: tuple[str, ...]
    autouse_by_node: Mapping[str, tuple[str, ...]]
    fixture_defs: Mapping[str, FixtureDef]
    fixture_registry: Mapping[str, tuple[FixtureDef, ...]]
    items: tuple[Item, ...]
    items_by_nodeid: Mapping[str, Item]

    def item(self, nodeid: str) -> Item:
        """The collected test with this nodeid. Raises `KeyError` if the suite has no such test."""
        try:
            return self.items_by_nodeid[nodeid]
        except KeyError:
            raise KeyError(f"No test {nodeid!r} was collected in this extraction.") from None

    def ini_value(self, name: str) -> str:
        """The `repr`ed value of ini key `name`, under either spelling pytest knows it by.

        pytest renames ini keys and keeps the old spelling as an alias, so a suite's config file
        and the dump can disagree about a setting's name; either name finds it. Raises `KeyError`
        when the extracted pytest had no such setting at all under any spelling, which is not the
        same as the setting being left at its default.
        """
        for candidate in (name, self.ini_aliases.get(name), *self._aliases_of(name)):
            if candidate is not None and candidate in self.ini:
                return self.ini[candidate]
        raise KeyError(
            f"pytest {self.pytest_version} registered no ini key named {name!r}, under that "
            "spelling or any it is aliased to."
        )

    def _aliases_of(self, name: str) -> tuple[str, ...]:
        return tuple(alias for alias, canonical in self.ini_aliases.items() if canonical == name)

    def resolve_path(
        self,
        path: str | None,
        *,
        prefix: str | None = None,
        base_prefix: str | None = None,
        site_packages: str | None = None,
    ) -> Path | None:
        """`path` as written in the dump, expanded against this suite's rootdir.

        A path carrying a `${site_packages}`, `${prefix}` or `${base_prefix}` token belongs to
        the environment the extraction ran in, not to the suite, and expands only if that
        directory is supplied. They are supplied separately because they differ: in a virtualenv
        the two prefixes are not the same, and the directory a package was installed into is not
        always under either.
        """
        if path is None:
            return None
        for token, replacement in (
            ("${site_packages}", site_packages),
            ("${prefix}", prefix),
            ("${base_prefix}", base_prefix),
        ):
            if path == token:
                return Path(replacement) if replacement else None
            if path.startswith(token + "/"):
                return Path(replacement, path[len(token) + 1 :]) if replacement else None
        if Path(path).is_absolute():
            return Path(path)
        return Path(self.rootpath, path)


def load(path: str | Path) -> GroundTruth:
    """The `GroundTruth` for the dump at `path`. Raises `DumpError` for a dump it cannot read."""
    return build(schema.load(path))


def build(dump: Mapping) -> GroundTruth:
    """The `GroundTruth` for an already-validated dump.

    Raises `DumpError` when a fixture chain names a definition the dump does not carry, which
    means the dump was edited or truncated after it was written.
    """
    fixture_defs = {key: _fixture_def(key, entry) for key, entry in dump["fixture_defs"].items()}

    def chain(keys: Sequence[str], where: str) -> tuple[FixtureDef, ...]:
        resolved = []
        for key in keys:
            found = fixture_defs.get(key)
            if found is None:
                raise DumpError(
                    f"{where} refers to fixture definition {key!r}, which the dump does not "
                    "define. The dump is inconsistent; extract again."
                )
            resolved.append(found)
        return tuple(resolved)

    registry = {
        name: chain(keys, f"The registry entry for {name!r}")
        for name, keys in dump["fixture_registry"].items()
    }
    items = tuple(_item(entry, chain) for entry in dump["items"])
    plugins = tuple(
        Plugin(
            name=entry.get("plugin", ""),
            dist=entry.get("dist", ""),
            version=entry.get("version", ""),
        )
        for entry in dump["plugins"].get("distinfo", ())
    )

    return GroundTruth(
        extractor_version=dump["extractor_version"],
        pytest_version=dump["pytest_version"],
        environment=dict(dump["environment"]),
        rootpath=dump["rootpath"],
        inipath=dump["inipath"],
        args=tuple(dump["args"]),
        ini=dict(dump["ini"]),
        ini_aliases=dict(dump["ini_aliases"]),
        plugins=plugins,
        plugin_names=tuple(dump["plugins"].get("names", ())),
        autouse_by_node={nodeid: tuple(names) for nodeid, names in dump["autouse_by_node"].items()},
        fixture_defs=fixture_defs,
        fixture_registry=registry,
        items=items,
        items_by_nodeid={item.nodeid: item for item in items},
    )


def _fixture_def(key: str, entry: Mapping) -> FixtureDef:
    params = entry["params"]
    ids = entry["ids"]
    func = entry["func"]
    return FixtureDef(
        key=key,
        argname=entry["argname"],
        scope=entry["scope"],
        params=None if params is None else tuple(params),
        ids=ids if isinstance(ids, str) or ids is None else tuple(ids),
        autouse=bool(entry["autouse"]),
        visibility=entry["visibility"],
        kind=entry["kind"],
        direct_param=bool(entry["direct_param"]),
        argnames=tuple(entry["argnames"]),
        returns=entry["returns"],
        func=FuncLocation(
            module=func.get("module"),
            qualname=func.get("qualname"),
            file=func.get("file"),
            lineno=func.get("lineno"),
            wrapped=bool(func.get("wrapped", False)),
        ),
    )


def _mark(entry: Mapping) -> Mark:
    return Mark(
        name=entry["name"],
        args=tuple(entry.get("args", ())),
        kwargs=dict(entry.get("kwargs", {})),
        origin=entry.get("from"),
    )


def _item(entry: Mapping, chain) -> Item:
    nodeid = entry["nodeid"]
    chains = {
        name: chain(keys, f"The chain for {name!r} in {nodeid}")
        for name, keys in entry.get("name2fixturedefs", {}).items()
    }
    callspec_entry = entry.get("callspec")
    callspec = (
        None
        if callspec_entry is None
        else CallSpec(
            id=callspec_entry["id"],
            idlist=tuple(callspec_entry.get("idlist", ())),
            params=dict(callspec_entry.get("params", {})),
            indices=dict(callspec_entry.get("indices", {})),
            marks=tuple(_mark(m) for m in callspec_entry.get("marks", ())),
        )
    )
    return Item(
        nodeid=nodeid,
        path=entry.get("path"),
        lineno=entry.get("lineno"),
        originalname=entry["originalname"],
        cls=entry.get("cls"),
        argnames=tuple(entry.get("argnames", ())),
        initialnames=tuple(entry.get("initialnames", ())),
        names_closure=tuple(entry.get("names_closure", ())),
        chains=chains,
        usefixtures=tuple(entry.get("usefixtures", ())),
        autouse_names=tuple(entry.get("autouse", ())),
        own_markers=tuple(_mark(m) for m in entry.get("own_markers", ())),
        markers_with_origin=tuple(_mark(m) for m in entry.get("markers_with_origin", ())),
        callspec=callspec,
    )
