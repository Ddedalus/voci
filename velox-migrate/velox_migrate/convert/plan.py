"""What this conversion translates, what it refuses, and the work order for each file.

The audit already classified every construct in the suite against the support matrix, so nothing
here re-decides what a construct is: a plan reads the audit's findings and sorts them into the two
outcomes a rewrite has — translate it, or leave it alone and say so in the source.

Refusal is the load-bearing half. A refused test keeps its pytest signature, which velox reports
as a collection error naming that test, so a partial conversion announces itself instead of
running tests that mean something new. Refusal also travels: a fixture nothing can translate
refuses every fixture downstream of it and every test that reaches it, because the alternative is
emitting a `Depends()` that names an object nobody built.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from velox_migrate import matrix
from velox_migrate.audit import Audit, Finding, Site, sources_of
from velox_migrate.audit import wiring as audit_wiring
from velox_migrate.convert import layout
from velox_migrate.convert.layout import Import, Layout
from velox_migrate.convert.rules import Context
from velox_migrate.model import REQUEST, FixtureDef, GroundTruth, Item

# Codes a later phase of the tool converts. Until then they behave exactly as a refusal: the
# construct is real, the translation is not written, and the source says so. Each is a support
# matrix row whose disposition already says conversion is possible, which is why the list lives
# here rather than in the matrix.
DEFERRED: frozenset[str] = frozenset(
    {
        "VX005",  # specialized override chains
        "VX007",  # indirect parametrization
        "VX008",  # autouse placement
        "VX009",  # usefixtures covering a module
        "VX010",  # usefixtures covering some of a module
        "VX011",  # getfixturevalue with a literal name
        "VX013",  # addfinalizer
        "VX024",  # cases a pytest_generate_tests hook produced
        "VX217",  # mock.patch as a decorator
        "VX218",  # mock.patch as a context manager
    }
)

# pytest's own fixtures with a velox counterpart, and the parameter name the translation binds.
# `capsys` and `caplog` are renamed because their velox counterparts are different objects with
# different methods, and a body reading `capsys.readouterr()` has to be rewritten anyway.
BUILTINS: Mapping[str, tuple[str, str]] = {
    "tmp_path": ("tmp_path", "velox.tmp_path"),
    "tmp_path_factory": ("tmp_path_factory", "velox.tmp_path_factory"),
    "capsys": ("capture", "velox.capture"),
    "caplog": ("log_records", "velox.log_records"),
}

# The velox scope a pytest scope becomes. velox has four, and the two pytest has that it does not
# widen to the next one out, which is `VX003`.
SCOPES: Mapping[str, str] = {
    "session": "session",
    "package": "session",
    "module": "module",
    "class": "module",
    "function": "function",
}

# pytest wraps a class or module lifecycle in a fixture of its own making; nobody wrote it, so
# there is no source to translate.
_SYNTHETIC = ("_xunit_", "_unittest_")


@dataclass(frozen=True, slots=True)
class Injection:
    """One parameter of a test or fixture factory, and what it becomes.

    `reference` is the expression the emitted `Depends()` wraps. `param` is the parameter's name
    afterwards, which differs from `was` only where the velox counterpart is a different object.
    """

    was: str
    param: str
    reference: str

    @property
    def renamed(self) -> bool:
        return self.param != self.was


@dataclass(frozen=True, slots=True)
class FixtureWork:
    """One fixture definition to translate, in the file its factory is written in."""

    key: str
    argname: str
    symbol: str
    scope: str
    injections: tuple[Injection, ...]
    parametrized: bool

    @property
    def request_param(self) -> Injection | None:
        """The `request` parameter a `params=` fixture reads its case from, once renamed."""
        return next((i for i in self.injections if i.was == REQUEST), None)


@dataclass(frozen=True, slots=True)
class TestWork:
    """One collected test to translate, named as it is written in its module."""

    qualname: str
    injections: tuple[Injection, ...]


@dataclass(frozen=True, slots=True)
class FileWork:
    """Everything one source file's rewrite needs.

    `target` differs from `path` for a `conftest.py`, whose whole content moves to the
    `fixtures.py` beside it. `marks` are the `VELOX-TODO` comments to attach, each keyed by the
    qualname it belongs above and empty for one about the module itself.
    """

    path: str
    target: str
    fixtures: tuple[FixtureWork, ...] = ()
    tests: tuple[TestWork, ...] = ()
    imports: tuple[Import, ...] = ()
    marks: tuple[tuple[str, str], ...] = ()
    context: Context = field(default_factory=lambda: Context(path=""))

    @property
    def moved(self) -> bool:
        return self.target != self.path


@dataclass(frozen=True, slots=True)
class Plan:
    """What one conversion of a suite does, decided before a single character is rewritten."""

    layout: Layout
    work: Mapping[str, FileWork]
    blocked_tests: frozenset[str]
    blocked_fixtures: frozenset[str]
    refusals: tuple[Finding, ...]
    markers: tuple[Finding, ...]

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(sorted(self.work))

    @property
    def moves(self) -> Mapping[str, str]:
        return self.layout.moves


def build(audit: Audit, ground_truth: GroundTruth, *, root: Path) -> Plan:
    """Plan the conversion of the suite `audit` describes, reading its sources from `root`."""
    sources = _sources(ground_truth, root)
    declared = _declared_fixtures(sources)

    translatable = _translatable(ground_truth)
    refusals, markers = _sorted_findings(audit)
    blocked_fixtures = _blocked_fixtures(ground_truth, translatable, refusals, declared)
    blocked_tests = _blocked_tests(ground_truth, refusals, blocked_fixtures)

    converting = {
        key: fixture for key, fixture in translatable.items() if key not in blocked_fixtures
    }
    items = _items_by_qualname(ground_truth, blocked_tests)
    plan_layout = layout.plan(
        converting,
        symbols={
            (file, argname): symbol
            for file, found in declared.items()
            for argname, symbol in found.items()
        },
        consumers=_consumers(ground_truth, converting, items, blocked_fixtures),
        source_of=sources.get,
    )
    return Plan(
        layout=plan_layout,
        work=_work(
            ground_truth,
            plan_layout,
            sources=sources,
            converting=converting,
            items=items,
            blocked_tests=blocked_tests,
            refusals=refusals,
            markers=markers,
        ),
        blocked_tests=blocked_tests,
        blocked_fixtures=frozenset(blocked_fixtures),
        refusals=refusals,
        markers=markers,
    )


def refused(finding: Finding) -> bool:
    """Whether this finding's construct is one the conversion leaves alone."""
    return not finding.construct.converts or finding.code in DEFERRED


def _sources(ground_truth: GroundTruth, root: Path) -> dict[str, str]:
    """Every Python file of the suite that `root` holds, by its rootdir-relative path.

    The whole suite, not just the files some finding points at: a test module with nothing wrong
    with it is exactly the file a conversion has the most to do in. A file the dump names and
    `root` does not hold is left out, which is what makes converting an already-converted tree a
    no-op for the conftests it has already moved away.
    """
    found: dict[str, str] = {}
    for path in sources_of(ground_truth, root=root):
        if not path.endswith(".py"):
            continue
        try:
            found[path] = Path(root, path).read_text(encoding="utf-8")
        except OSError:
            continue
    return found


def _sorted_findings(audit: Audit) -> tuple[tuple[Finding, ...], tuple[Finding, ...]]:
    """The audit's findings split into the ones that stop a rewrite and the ones that caveat it."""
    refusals = tuple(f for f in audit.findings if refused(f))
    markers = tuple(
        f
        for f in audit.findings
        if not refused(f) and f.construct.disposition is matrix.Disposition.MARKER
    )
    return refusals, markers


def _translatable(ground_truth: GroundTruth) -> dict[str, FixtureDef]:
    """Every fixture written in the suite that some test reaches, keyed as the dump keys it."""
    return {
        key: fixture
        for key, fixture in audit_wiring.reached(ground_truth).items()
        if audit_wiring.in_suite(fixture)
        and not fixture.direct_param
        and not fixture.argname.startswith(_SYNTHETIC)
    }


def _declared_fixtures(sources: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """Per file, the argname of each `@pytest.fixture` in it against the name it is written under.

    The dump records where a fixture's *factory* is, which a decorator can move to another module
    entirely, so the binding name only the source can give.

    Only module-level definitions count. A fixture written inside a class or another function binds
    no name a `Depends()` could import, and leaving it out of this table is what refuses it.
    """
    declared: dict[str, dict[str, str]] = {}
    for path, text in sources.items():
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            continue
        found: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            argname = _fixture_argname(node)
            if argname is not None:
                found[argname] = node.name
        if found:
            declared[path] = found
    return declared


def _fixture_argname(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The name a `@pytest.fixture` function is requested by, or `None` if it is not a fixture."""
    for decorator in node.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        target = call.func if call is not None else decorator
        if not (isinstance(target, ast.Attribute) and target.attr == "fixture") and not (
            isinstance(target, ast.Name) and target.id == "fixture"
        ):
            continue
        if call is not None:
            for keyword in call.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    value = keyword.value.value
                    return value if isinstance(value, str) else node.name
        return node.name
    return None


def _fixtures_by_site(ground_truth: GroundTruth) -> Mapping[tuple[str, str], frozenset[str]]:
    """Fixture keys by the file and qualname their factory is written at."""
    found: dict[tuple[str, str], set[str]] = {}
    for key, fixture in ground_truth.fixture_defs.items():
        file, qualname = fixture.func.file, fixture.func.qualname
        if file is not None and qualname is not None:
            found.setdefault((file, qualname), set()).add(key)
    return {site: frozenset(keys) for site, keys in found.items()}


def _blocked_fixtures(
    ground_truth: GroundTruth,
    translatable: Mapping[str, FixtureDef],
    refusals: tuple[Finding, ...],
    declared: Mapping[str, Mapping[str, str]],
) -> set[str]:
    """Every fixture this conversion leaves as pytest wrote it.

    Three reasons, and the third is why this is a fixpoint: a refusal landed inside its factory;
    nothing in the suite binds an object to import for it; or something it depends on is already
    blocked, which makes a correct `Depends()` impossible to write.
    """
    by_site = _fixtures_by_site(ground_truth)
    blocked: set[str] = set()

    for finding in refusals:
        site = finding.site
        if site.file is None:
            continue
        blocked |= _at_site(by_site, site)
        named = str(finding.detail.get("fixture", ""))
        if named:
            blocked |= {
                key
                for key, fixture in translatable.items()
                if fixture.argname == named and layout.owning_file(fixture) == site.file
            }

    for key, fixture in translatable.items():
        source = layout.owning_file(fixture)
        if source is None or fixture.argname not in declared.get(source, {}):
            # No source declares it under a name a `Depends()` could import.
            blocked.add(key)

    return _propagate(ground_truth, translatable, blocked)


def _at_site(by_site: Mapping[tuple[str, str], frozenset[str]], site: Site) -> frozenset[str]:
    """The fixtures whose factory encloses `site`, innermost name first."""
    enclosing = site.function
    while enclosing and site.file is not None:
        found = by_site.get((site.file, enclosing))
        if found:
            return found
        enclosing, _, _ = enclosing.rpartition(".")
    return frozenset()


def _propagate(
    ground_truth: GroundTruth, translatable: Mapping[str, FixtureDef], blocked: set[str]
) -> set[str]:
    """Grow `blocked` until every fixture depending on a blocked one is blocked too."""
    edges: dict[str, set[str]] = {}
    for item in ground_truth.items:
        for fixture in item.walk():
            if fixture.key not in translatable:
                # A `parametrize` mark's desugaring, a lifecycle wrapper pytest invented, or a
                # fixture from pytest itself: none has source in the suite to rewrite, and what
                # depending on one costs is decided where the dependency is read.
                continue
            requested = edges.setdefault(fixture.key, set())
            for edge in item.dependencies(fixture):
                if edge.fixture is not None:
                    requested.add(edge.fixture.key)
                elif edge.name != REQUEST:
                    # A name the dump cannot resolve is a dependency nothing can name.
                    blocked.add(fixture.key)
            if REQUEST in fixture.argnames and not fixture.is_parametrized:
                # `request` has no counterpart except as a `params=` fixture's own case.
                blocked.add(fixture.key)

    changed = True
    while changed:
        changed = False
        for key, requested in edges.items():
            if key in blocked:
                continue
            if _unnameable(ground_truth, requested, translatable, blocked):
                blocked.add(key)
                changed = True
    return blocked


def _unnameable(
    ground_truth: GroundTruth,
    requested: set[str],
    translatable: Mapping[str, FixtureDef],
    blocked: set[str],
) -> bool:
    """Whether any of `requested` is a dependency no `Depends()` can name.

    Either it is already refused, or it is a fixture from outside the suite with no velox
    counterpart — a plugin's, or one of pytest's own that velox does not provide.
    """
    for key in requested:
        if key in blocked:
            return True
        if key in translatable:
            continue
        fixture = ground_truth.fixture_defs.get(key)
        if fixture is None or fixture.direct_param:
            continue
        if fixture.argname not in BUILTINS:
            return True
    return False


def _blocked_tests(
    ground_truth: GroundTruth, refusals: tuple[Finding, ...], blocked_fixtures: set[str]
) -> frozenset[str]:
    blocked = {
        nodeid
        for finding in refusals
        if not finding.construct.suite_level
        for nodeid in finding.tests
    }
    for item in ground_truth.items:
        if any(fixture.key in blocked_fixtures for fixture in item.walk()):
            blocked.add(item.nodeid)
    return frozenset(blocked)


def _items_by_qualname(
    ground_truth: GroundTruth, blocked_tests: frozenset[str]
) -> Mapping[tuple[str, str], tuple[Item, ...]]:
    """The cases of each test function, by its file and the qualname it is written under.

    Every case of a test resolves the same fixtures once overrides are refused, so the first case
    answers the wiring questions and the whole tuple answers the parametrize ones.
    """
    grouped: dict[tuple[str, str], list[Item]] = {}
    for item in ground_truth.items:
        if item.path is None or item.nodeid in blocked_tests:
            continue
        qualname = f"{item.cls}.{item.originalname}" if item.cls else item.originalname
        grouped.setdefault((item.path, qualname), []).append(item)
    return {key: tuple(items) for key, items in grouped.items()}


def _consumers(
    ground_truth: GroundTruth,
    converting: Mapping[str, FixtureDef],
    items: Mapping[tuple[str, str], tuple[Item, ...]],
    blocked_fixtures: set[str],
) -> dict[str, set[str]]:
    """Per file, every fixture key the code in it will name in a `Depends()`."""
    consumers: dict[str, set[str]] = {}
    for (path, _), cases in items.items():
        wanted = consumers.setdefault(path, set())
        item = cases[0]
        for name in item.argnames:
            resolved = item.resolve(name)
            if resolved is not None and resolved.key in converting:
                wanted.add(resolved.key)

    for key, fixture in converting.items():
        source = layout.owning_file(fixture)
        if source is None:
            continue
        wanted = consumers.setdefault(layout.home_module(source), set())
        for edge in _edges_of(ground_truth, key, blocked_fixtures):
            if edge in converting:
                wanted.add(edge)
    return consumers


def _edges_of(ground_truth: GroundTruth, key: str, blocked_fixtures: set[str]) -> frozenset[str]:
    """The fixture keys `key` depends on, as every test that reaches it resolves them."""
    found: set[str] = set()
    for item in ground_truth.items:
        fixture = next((f for f in item.walk() if f.key == key), None)
        if fixture is None:
            continue
        for edge in item.dependencies(fixture):
            if edge.fixture is not None and edge.fixture.key not in blocked_fixtures:
                found.add(edge.fixture.key)
    return frozenset(found)


def _work(
    ground_truth: GroundTruth,
    plan_layout: Layout,
    *,
    sources: Mapping[str, str],
    converting: Mapping[str, FixtureDef],
    items: Mapping[tuple[str, str], tuple[Item, ...]],
    blocked_tests: frozenset[str],
    refusals: tuple[Finding, ...],
    markers: tuple[Finding, ...],
) -> dict[str, FileWork]:
    fixtures_by_file: dict[str, list[FixtureWork]] = {}
    for key, fixture in sorted(converting.items()):
        source = layout.owning_file(fixture)
        home = plan_layout.home(key)
        if source is None or home is None:
            continue
        fixtures_by_file.setdefault(source, []).append(
            FixtureWork(
                key=key,
                argname=fixture.argname,
                symbol=home.symbol,
                scope=SCOPES.get(fixture.scope, "function"),
                injections=_injections(ground_truth, plan_layout, key, home.module, converting),
                parametrized=fixture.is_parametrized,
            )
        )

    tests_by_file: dict[str, list[TestWork]] = {}
    for (path, qualname), cases in sorted(items.items()):
        tests_by_file.setdefault(path, []).append(
            TestWork(
                qualname=qualname,
                injections=_test_injections(cases[0], plan_layout, path, converting),
            )
        )

    marks = _marks(refusals, markers)
    paths = set(fixtures_by_file) | set(tests_by_file) | set(marks)
    work: dict[str, FileWork] = {}
    for path in sorted(paths):
        if path not in sources:
            continue
        target = plan_layout.moves.get(path, path)
        work[path] = FileWork(
            path=path,
            target=target,
            fixtures=tuple(fixtures_by_file.get(path, ())),
            tests=tuple(tests_by_file.get(path, ())),
            imports=plan_layout.imports_for(target),
            marks=tuple(sorted(marks.get(path, set()))),
            context=_context(ground_truth, path, items, blocked_tests),
        )
    return work


def _injections(
    ground_truth: GroundTruth,
    plan_layout: Layout,
    key: str,
    consumer: str,
    converting: Mapping[str, FixtureDef],
) -> tuple[Injection, ...]:
    """What each parameter of the fixture `key`'s factory becomes."""
    fixture = ground_truth.fixture_defs[key]
    resolved: dict[str, FixtureDef | None] = {}
    for item in ground_truth.items:
        found = next((f for f in item.walk() if f.key == key), None)
        if found is None:
            continue
        for edge in item.dependencies(found):
            resolved.setdefault(edge.name, edge.fixture)
        break
    return _from_names(
        fixture.argnames, resolved, plan_layout, consumer, converting, fixture.is_parametrized
    )


def _test_injections(
    item: Item,
    plan_layout: Layout,
    consumer: str,
    converting: Mapping[str, FixtureDef],
) -> tuple[Injection, ...]:
    resolved = {name: item.resolve(name) for name in item.argnames}
    return _from_names(item.argnames, resolved, plan_layout, consumer, converting, False)


def _from_names(
    names: tuple[str, ...],
    resolved: Mapping[str, FixtureDef | None],
    plan_layout: Layout,
    consumer: str,
    converting: Mapping[str, FixtureDef],
    parametrized: bool,
) -> tuple[Injection, ...]:
    found: list[Injection] = []
    for name in names:
        if name == REQUEST:
            if parametrized:
                found.append(Injection(was=REQUEST, param="param", reference=""))
            continue
        fixture = resolved.get(name)
        if fixture is None or fixture.direct_param:
            continue
        if fixture.key in converting:
            home = plan_layout.home(fixture.key)
            if home is None:
                continue
            imported = plan_layout.importing(consumer, fixture.key)
            reference = imported.bound if imported is not None else home.symbol
            found.append(Injection(was=name, param=name, reference=reference))
            continue
        builtin = BUILTINS.get(fixture.argname)
        if builtin is not None:
            param, reference = builtin
            found.append(Injection(was=name, param=param, reference=reference))
    return tuple(found)


def _marks(
    refusals: tuple[Finding, ...], markers: tuple[Finding, ...]
) -> dict[str, set[tuple[str, str]]]:
    """The `VELOX-TODO` comments to attach, by file, as (qualname, code) pairs."""
    found: dict[str, set[tuple[str, str]]] = {}
    for finding in (*refusals, *markers):
        file = finding.site.file
        if file is None or not file.endswith(".py"):
            continue
        found.setdefault(file, set()).add((finding.site.function or "", finding.code))
    return found


def _context(
    ground_truth: GroundTruth,
    path: str,
    items: Mapping[tuple[str, str], tuple[Item, ...]],
    blocked_tests: frozenset[str],
) -> Context:
    tests = {qualname for (file, qualname) in items if file == path}
    blocked = {
        f"{item.cls}.{item.originalname}" if item.cls else item.originalname
        for item in ground_truth.items
        if item.path == path and item.nodeid in blocked_tests
    }
    return Context(
        path=path,
        axis_ids=_axis_ids(items, path),
        tests=frozenset(tests),
        blocked=frozenset(blocked),
        xfail_strict=_xfail_strict(ground_truth),
    )


def _axis_ids(
    items: Mapping[tuple[str, str], tuple[Item, ...]], path: str
) -> Mapping[tuple[str, str], tuple[str, ...]]:
    """pytest's own ids for each parametrize axis in `path`, where one axis explains them.

    A case id is composed from every axis that varies the test, so an id can only be written onto
    one `parametrize` mark when exactly one position in pytest's own id list moves with that
    axis's index and stands still within it. Where no position does, the axis is `VX114` and the
    ids are velox's to generate.
    """
    found: dict[tuple[str, str], tuple[str, ...]] = {}
    for (file, qualname), cases in items.items():
        if file != path:
            continue
        specs = [item.callspec for item in cases if item.callspec is not None]
        if len(specs) != len(cases) or not specs:
            continue
        for argnames in _axes(specs):
            ids = _ids_for_axis(specs, argnames)
            if ids is not None:
                found[(qualname, ",".join(argnames))] = ids
    return found


def _axes(specs: list) -> Iterator[tuple[str, ...]]:
    """Every set of argnames that moves together across the cases.

    One `parametrize` mark over `"a,b"` gives `a` and `b` the same index in every case, so the
    names that share an index everywhere are the names one mark covers. They are kept in the order
    the dump lists them, which is the order pytest registered them and so the order the mark was
    written in — the order a rule looking an axis up by its argnames spells them in.
    """
    names = list(specs[0].indices)
    grouped: dict[tuple[int, ...], list[str]] = {}
    for name in names:
        signature = tuple(spec.indices.get(name, -1) for spec in specs)
        grouped.setdefault(signature, []).append(name)
    for group in grouped.values():
        yield tuple(group)


def _ids_for_axis(specs: list, argnames: tuple[str, ...]) -> tuple[str, ...] | None:
    by_index: dict[int, list] = {}
    for spec in specs:
        index = spec.indices.get(argnames[0])
        if index is None:
            return None
        by_index.setdefault(index, []).append(spec)
    if len(by_index) < 2 and len(specs) > 1:
        # An axis with one value explains nothing about which id part is its own.
        return None
    width = len(specs[0].idlist)
    if any(len(spec.idlist) != width for spec in specs):
        return None
    candidates = [
        position
        for position in range(width)
        if _constant_within(by_index, position) and _distinct_across(by_index, position)
    ]
    if len(candidates) != 1:
        return None
    position = candidates[0]
    return tuple(by_index[index][0].idlist[position] for index in sorted(by_index))


def _constant_within(by_index: Mapping[int, list], position: int) -> bool:
    return all(len({spec.idlist[position] for spec in group}) == 1 for group in by_index.values())


def _distinct_across(by_index: Mapping[int, list], position: int) -> bool:
    seen = [group[0].idlist[position] for group in by_index.values()]
    return len(set(seen)) == len(seen)


def _xfail_strict(ground_truth: GroundTruth) -> bool:
    try:
        return ast.literal_eval(ground_truth.ini_value("xfail_strict")) is True
    except (KeyError, ValueError, SyntaxError):
        return False
