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
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from velox_migrate import matrix
from velox_migrate.audit import Audit, Finding, Site, sources_of
from velox_migrate.audit import wiring as audit_wiring
from velox_migrate.convert import declarations, layout, specialize
from velox_migrate.convert.declarations import Declaration
from velox_migrate.convert.layout import Import, Layout
from velox_migrate.convert.rules import Context
from velox_migrate.convert.specialize import Specialization
from velox_migrate.model import REQUEST, FixtureDef, GroundTruth, Item

# Codes a later phase of the tool converts. Until then they behave exactly as a refusal: the
# construct is real, the translation is not written, and the source says so. Each is a support
# matrix row whose disposition already says conversion is possible, which is why the list lives
# here rather than in the matrix.
DEFERRED: frozenset[str] = frozenset(
    {
        "VX007",  # indirect parametrization
        "VX024",  # cases a pytest_generate_tests hook produced
    }
)

# What the body scan says about `request` in one definition: the two shapes a rewrite answers, the
# rows that mean the parameter outlives the rewrite whatever else is written beside them, and the
# row each of the two is refused under where the shape it is written in has no rewrite.
_DYNAMIC = ("VX011", "VX013")
_REQUEST_SURVIVES = ("VX012", "VX014", "VX015", "VX016", "VX017")
_DECLINED = {"VX011": "VX028", "VX013": "VX014"}

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
    afterwards, which differs from `was` only where the velox counterpart is a different object,
    and is empty for the one injection that is a removal: a `request` every use of which the
    rewrite has taken away. `asked` marks the injection a body asked for by name rather than
    through a parameter, which is the one the signature grows a parameter for.
    """

    was: str
    param: str
    reference: str
    asked: bool = False

    @property
    def renamed(self) -> bool:
        return self.param != self.was

    @property
    def dropped(self) -> bool:
        """Whether this injection takes its parameter away rather than binding it."""
        return not self.param


@dataclass(frozen=True, slots=True)
class Body:
    """What the scan found inside one definition, attributed to the definition it sits in.

    `requested` are the fixtures a literal `request.getfixturevalue` asks for, each as the name it
    was asked for and the key it resolves to: a name decided in source is a static dependency, so
    it becomes an ordinary injected parameter. `finalizers` says this definition's
    `request.addfinalizer` calls become the teardown after its `yield`. Either one is a use of
    `request` the rewrite takes away, and a definition whose every use it takes away is one the
    parameter itself leaves.
    """

    requested: tuple[tuple[str, str], ...] = ()
    finalizers: bool = False


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
class Duplicate:
    """One fixture's source, re-bound for the subtree an override rules, to write into a module.

    `after` are the names this copy references, which is what decides where it goes: a `Depends()`
    is read when the `def` under it is, so a copy is written below everything it names.
    """

    symbol: str
    code: str
    after: tuple[str, ...]


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
    qualname it belongs above and empty for one about the module itself. `declares` are the
    fixtures this file names in a `velox.use(...)`, as the expressions naming them here, and a
    file with declarations and nothing else is one the conversion writes from nothing.
    `duplicates` are specialized copies appended to the target before anything else runs, so the
    rewrite translates them exactly as it translates the definitions already written there, and
    `needs` are the modules those copies read plainly. `solo` are the tests in this file that take
    the whole run to themselves, because a patch entered inside a body is one nothing outside the
    test can see.
    """

    path: str
    target: str
    fixtures: tuple[FixtureWork, ...] = ()
    tests: tuple[TestWork, ...] = ()
    imports: tuple[Import, ...] = ()
    marks: tuple[tuple[str, str], ...] = ()
    declares: tuple[str, ...] = ()
    duplicates: tuple[Duplicate, ...] = ()
    needs: tuple[str, ...] = ()
    solo: tuple[str, ...] = ()
    context: Context = field(default_factory=lambda: Context(path=""))

    @property
    def moved(self) -> bool:
        return self.target != self.path


@dataclass(frozen=True, slots=True)
class Plan:
    """What one conversion of a suite does, decided before a single character is rewritten.

    `packages` are the `__init__.py` files a declaration needs in place to be read at all, empty
    ones included: velox walks up from a test file and stops at the first directory that is not a
    package, so a gap in the chain is a declaration that silently reaches nothing.
    """

    layout: Layout
    work: Mapping[str, FileWork]
    blocked_tests: frozenset[str]
    blocked_fixtures: frozenset[str]
    refusals: tuple[Finding, ...]
    markers: tuple[Finding, ...]
    declarations: tuple[Declaration, ...] = ()
    packages: tuple[str, ...] = ()
    specialized: Specialization = specialize.NONE

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
    overrides = audit_wiring.overrides(ground_truth)

    # What a body asked for by name, attributed to the definition that asked: a site the rewrite
    # cannot answer for is a refusal like any other, and travels like one.
    bodies, unattributed = _bodies(
        audit,
        ground_truth,
        declared=declared,
        yieldable=_yieldable_factories(sources),
        items=_items_by_qualname(ground_truth, frozenset()),
        translatable=translatable,
    )
    refusals = (*refusals, *unattributed)

    blocked_fixtures = _blocked_fixtures(
        ground_truth, translatable, refusals, declared, overrides, bodies
    )
    blocked_tests = _blocked_tests(ground_truth, refusals, blocked_fixtures, bodies)

    converting = {
        key: fixture for key, fixture in translatable.items() if key not in blocked_fixtures
    }
    items = _items_by_qualname(ground_truth, blocked_tests)
    symbols = {
        (file, argname): symbol
        for file, found in declared.items()
        for argname, symbol in found.items()
    }
    plan_layout = layout.plan(
        converting,
        symbols=symbols,
        consumers=_consumers(ground_truth, converting, items, (), specialize.NONE, bodies),
        source_of=sources.get,
    )

    # A fixture the layout could not place has nowhere to be imported from, which refuses it and
    # everything that reaches it exactly as any other refusal does.
    unplaced = {key for key in converting if plan_layout.home(key) is None}
    if unplaced:
        blocked_fixtures = _propagate(
            ground_truth, translatable, blocked_fixtures | unplaced, bodies
        )
        blocked_tests = _blocked_tests(ground_truth, refusals, blocked_fixtures, bodies)
        converting = {
            key: fixture for key, fixture in translatable.items() if key not in blocked_fixtures
        }
        items = _items_by_qualname(ground_truth, blocked_tests)

    # A body belongs to a definition, so a definition this conversion is leaving as pytest wrote
    # it has no body here either: what the rules read is what the rewrite is going to write.
    bodies = _converting_bodies(bodies, ground_truth, converting, items)

    # Specialization comes after every refusal is settled, since a chain is only worth copying
    # where every fixture in it converts, and before the layout, which places the copies.
    special = specialize.plan(
        ground_truth,
        overrides,
        converting=converting,
        symbols=symbols,
        source_of=sources.get,
        taken={
            layout.home_module(file): layout.module_level_names(text)
            for file, text in sources.items()
        },
    )

    # Declarations are placed once every refusal is settled, since a refused test is one nothing is
    # declared for — and then the layout is planned again, because a declaring module names the
    # fixtures it declares and so needs their imports like any other consumer. Placing fixtures is
    # not affected: what a second pass adds is imports.
    placed = declarations.plan(ground_truth, available=converting, blocked=blocked_tests)
    _read_containers(placed, sources, root)
    plan_layout = layout.plan(
        converting,
        symbols=symbols,
        consumers=_consumers(ground_truth, converting, items, placed, special, bodies),
        source_of=sources.get,
        placed=special.homes,
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
            placed=placed,
            special=special,
            bodies=bodies,
            solo=_solo(audit, items),
        ),
        blocked_tests=blocked_tests,
        blocked_fixtures=frozenset(blocked_fixtures),
        refusals=refusals,
        markers=markers,
        declarations=placed,
        packages=declarations.packages(ground_truth, placed, blocked=blocked_tests),
        specialized=special,
    )


def refused(finding: Finding) -> bool:
    """Whether this finding's construct is one the conversion leaves alone.

    A hazard is not one of them. It is a behaviour change concurrency causes rather than syntax, so
    the construct converts and the report says which tests end up needing `@velox.solo` — refusing
    a fixture over an `os.environ` write in it would refuse most of a suite for something the
    rewrite is not what fixes.
    """
    return finding.code in DEFERRED or finding.disposition in (
        matrix.Disposition.REFUSED,
        matrix.Disposition.UNSUPPORTED,
    )


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


def _read_containers(placed: Sequence[Declaration], sources: dict[str, str], root: Path) -> None:
    """Add every declaration container to `sources`, empty for one that is not there yet.

    A package `__init__.py` is usually a file the conversion creates, and the layout still has to
    know what it binds: a suite that already has one, holding names of its own, is where an
    imported fixture needs an alias exactly as in any other module.
    """
    for declaration in placed:
        if declaration.container in sources:
            continue
        try:
            sources[declaration.container] = Path(root, declaration.container).read_text(
                encoding="utf-8"
            )
        except OSError:
            sources[declaration.container] = ""


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


def _yieldable_factories(sources: Mapping[str, str]) -> Mapping[str, frozenset[str]]:
    """Per file, the module-level fixture factories a finalizer could become the teardown of.

    A `yield` fixture hands its value over at one point in the body and tears down after it, so a
    factory has to have one place that point can go: no `yield` of its own, and no `return` except
    as the last thing it does. A `return` from inside a branch would become a `yield` the body
    then runs past.
    """
    found: dict[str, frozenset[str]] = {}
    for path, text in sources.items():
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            continue
        names = {
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and _fixture_argname(node) is not None
            and _yieldable(node)
        }
        if names:
            found[path] = frozenset(names)
    return found


def _yieldable(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    own = list(_own_nodes(node))
    if any(isinstance(found, ast.Yield | ast.YieldFrom) for found in own):
        return False
    trailing = node.body[-1] if node.body else None
    return all(found is trailing for found in own if isinstance(found, ast.Return))


def _own_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Every node inside `node` that belongs to it rather than to a function written within it."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        yield child
        stack.extend(ast.iter_child_nodes(child))


def _bodies(
    audit: Audit,
    ground_truth: GroundTruth,
    *,
    declared: Mapping[str, Mapping[str, str]],
    yieldable: Mapping[str, frozenset[str]],
    items: Mapping[tuple[str, str], tuple[Item, ...]],
    translatable: Mapping[str, FixtureDef],
) -> tuple[Mapping[tuple[str, str], Body], tuple[Finding, ...]]:
    """What each body asked for by name, keyed by site, and the findings no definition can answer.

    A dynamic request is only translatable where the rewrite has a signature to write it into and
    a `request` it can take away whole: a use of `request` the rewrite has no answer for leaves
    the parameter behind, and a parameter left behind is a fixture velox cannot construct. So a
    site is attributed only when it is a definition this conversion owns and nothing else in it
    reads `request`, and refused where it is a definition this conversion writes and none of that
    holds. A site that is neither — a helper, or a fixture no test reaches — is left alone: there
    is nothing there for a refusal to protect.
    """
    by_site = _fixtures_by_site(ground_truth)
    surviving = {
        (finding.site.file or "", finding.site.function or "")
        for finding in audit.findings
        if finding.code in _REQUEST_SURVIVES
    }
    writing = {
        (fixture.func.file or "", fixture.func.qualname or "") for fixture in translatable.values()
    } | set(items)
    bodies: dict[tuple[str, str], Body] = {}
    unattributed: list[Finding] = []
    for finding in sorted(audit.findings, key=lambda finding: finding.site.sort_key):
        if finding.code not in _DYNAMIC:
            continue
        file, function = finding.site.file or "", finding.site.function or ""
        site = (file, function)
        node = _asking_node(ground_truth, by_site, items, site)
        owned = (
            function in yieldable.get(file, frozenset())
            if finding.code == "VX013"
            else function in declared.get(file, {}).values() or site in items
        )
        wanted = _requested(ground_truth, finding, node, translatable)
        if site not in writing:
            continue
        if not owned or site in surviving or wanted is None:
            unattributed.append(_declined(finding, owned=owned and site not in surviving))
            continue
        body = bodies.get(site, Body())
        bodies[site] = Body(
            requested=body.requested + wanted,
            finalizers=body.finalizers or finding.code == "VX013",
        )
    return bodies, tuple(unattributed)


def _declined(finding: Finding, *, owned: bool) -> Finding:
    """`finding` under the row that refuses it, saying which of the two reasons it is.

    Either the definition it sits in is not one this conversion writes a signature for, or the
    name it asks for is not one a single parameter can stand for.
    """
    name = str(finding.detail.get("requested", ""))
    asked = f'`request.getfixturevalue("{name}")`' if name else "`request.getfixturevalue(...)`"
    message = (
        f"{asked} asks for a name this suite defines in more than one directory, and a parameter "
        "names one object."
        if owned
        else f"{asked} is asked for where there is no signature for the parameter to go in."
    )
    if finding.code == "VX013":
        message = (
            "`request.addfinalizer(...)` is registered where no single `yield` can take its place."
        )
    return Finding(
        code=_DECLINED[finding.code],
        message=message,
        site=finding.site,
        tests=finding.tests,
        detail=finding.detail,
    )


def _requested(
    ground_truth: GroundTruth,
    finding: Finding,
    node: str | None,
    translatable: Mapping[str, FixtureDef],
) -> tuple[tuple[str, str], ...] | None:
    """The name and key one `getfixturevalue` finding resolves to, or `None` if it resolves to none.

    A name the whole suite defines once is a static dependency wherever it is asked for. A name two
    directories define is the override question, which pytest answers per test and an injected
    parameter cannot answer at all — the object a body reaches is decided where that body is
    written, so a rewrite of it would hand one subtree's definition to another.

    The definition it names also has to be one a `Depends()` can name: a fixture this conversion
    writes an object for, or one of pytest's own that velox provides under another name.
    """
    if finding.code != "VX011":
        return ()
    name = str(finding.detail.get("requested", ""))
    chain = ground_truth.fixture_registry.get(name, ())
    if not name or node is None or len(chain) != 1:
        return None
    found = chain[0]
    if not audit_wiring.under(found.visibility, node):
        return None
    nameable = found.key in translatable or found.argname in BUILTINS
    return ((name, found.key),) if nameable else None


def _asking_node(
    ground_truth: GroundTruth,
    by_site: Mapping[tuple[str, str], frozenset[str]],
    items: Mapping[tuple[str, str], tuple[Item, ...]],
    site: tuple[str, str],
) -> str | None:
    """The node a name asked for at `site` is resolved from: a fixture's own, or a test's."""
    keys = by_site.get(site)
    if keys:
        return ground_truth.fixture_defs[sorted(keys)[0]].visibility
    cases = items.get(site)
    return cases[0].nodeid if cases else None


def _converting_bodies(
    bodies: Mapping[tuple[str, str], Body],
    ground_truth: GroundTruth,
    converting: Mapping[str, FixtureDef],
    items: Mapping[tuple[str, str], tuple[Item, ...]],
) -> Mapping[tuple[str, str], Body]:
    """`bodies`, less every site whose definition this conversion turned out to be leaving alone."""
    written = {
        (fixture.func.file or "", fixture.func.qualname or "") for fixture in converting.values()
    }
    written |= set(items)
    return {site: body for site, body in bodies.items() if site in written}


def _body_of(bodies: Mapping[tuple[str, str], Body], fixture: FixtureDef) -> Body | None:
    return bodies.get((fixture.func.file or "", fixture.func.qualname or ""))


def _body_of_item(bodies: Mapping[tuple[str, str], Body], item: Item) -> Body | None:
    return bodies.get((item.path or "", _qualname_of(item)))


def _qualname_of(item: Item) -> str:
    return f"{item.cls}.{item.originalname}" if item.cls else item.originalname


def _solo(
    audit: Audit, items: Mapping[tuple[str, str], tuple[Item, ...]]
) -> Mapping[str, frozenset[str]]:
    """The tests that take the whole run to themselves, by the file they are written in.

    Read from the finding's blast radius rather than from its site: a patch entered inside a body
    is invisible until it runs, and the body that enters it may be a fixture's, so what has to run
    alone is every test that reaches it. A test this conversion is refusing is left out — it keeps
    the source pytest ran, and a mark on it would say nothing about how velox schedules it.
    """
    alone = {
        nodeid for finding in audit.findings if finding.code == "VX218" for nodeid in finding.tests
    }
    found: dict[str, set[str]] = {}
    for (path, qualname), cases in items.items():
        if any(item.nodeid in alone for item in cases):
            found.setdefault(path, set()).add(qualname)
    return {path: frozenset(names) for path, names in found.items()}


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
    overrides: Sequence[audit_wiring.Override],
    bodies: Mapping[tuple[str, str], Body],
) -> set[str]:
    """Every fixture this conversion leaves as pytest wrote it.

    Four reasons, and the last is why this is a fixpoint: a refusal landed inside its factory;
    its chain runs an autouse fixture, which makes both definitions of the overridden name
    undeclarable; nothing in the suite binds an object to import for it; or something it depends
    on is already blocked, which makes a correct `Depends()` impossible to write.
    """
    by_site = _fixtures_by_site(ground_truth)
    blocked: set[str] = _undeclarable(overrides)

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

    return _propagate(ground_truth, translatable, blocked, bodies)


def _undeclarable(overrides: Sequence[audit_wiring.Override]) -> set[str]:
    """The overriding definitions whose subtree a declaration cannot be written for.

    A `velox.use(...)` names one object for a directory, so an autouse fixture that an override
    changes would be declared once for each definition over tests that had exactly one. Only the
    overriding definition is named here; the refusal then travels as any other does, through
    everything that depends on the overridden name.
    """
    return {override.winner.key for override in overrides if override.autouse}


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
    ground_truth: GroundTruth,
    translatable: Mapping[str, FixtureDef],
    blocked: set[str],
    bodies: Mapping[tuple[str, str], Body],
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
            body = _body_of(bodies, fixture)
            if body is not None:
                # A name a body asked for is a dependency like any other, so a refusal reaches
                # this fixture through it exactly as it does through a parameter.
                requested |= {key for _, key in body.requested}
            for edge in item.dependencies(fixture):
                if edge.fixture is not None:
                    requested.add(edge.fixture.key)
                elif edge.name != REQUEST:
                    # A name the dump cannot resolve is a dependency nothing can name.
                    blocked.add(fixture.key)
            if REQUEST in fixture.argnames and not fixture.is_parametrized and body is None:
                # `request` has no counterpart except as a `params=` fixture's own case, or where
                # the scan accounted for every use of it and the rewrite takes them all away.
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
    ground_truth: GroundTruth,
    refusals: tuple[Finding, ...],
    blocked_fixtures: set[str],
    bodies: Mapping[tuple[str, str], Body],
) -> frozenset[str]:
    blocked = {
        nodeid
        for finding in refusals
        if not finding.construct.suite_level
        for nodeid in finding.tests
    }
    for item in ground_truth.items:
        body = _body_of_item(bodies, item)
        reached = {fixture.key for fixture in item.walk()}
        if body is not None:
            # A fixture this test asked for by name is not in its closure, so nothing else here
            # would notice that the object it names is one the conversion is not writing.
            reached |= {key for _, key in body.requested}
        if reached & blocked_fixtures:
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
        grouped.setdefault((item.path, _qualname_of(item)), []).append(item)
    return {key: tuple(items) for key, items in grouped.items()}


def _consumers(
    ground_truth: GroundTruth,
    converting: Mapping[str, FixtureDef],
    items: Mapping[tuple[str, str], tuple[Item, ...]],
    placed: Sequence[Declaration],
    special: Specialization,
    bodies: Mapping[tuple[str, str], Body] = {},
) -> dict[str, set[str]]:
    """Per file, every fixture key the code in it will name — in a `Depends()` or a declaration."""
    consumers: dict[str, set[str]] = {}
    for declaration in placed:
        consumers.setdefault(declaration.container, set()).update(
            special.redirect(declaration.container, key) for key in declaration.keys
        )
    for (path, _), cases in items.items():
        wanted = consumers.setdefault(path, set())
        item = cases[0]
        for name in item.argnames:
            resolved = item.resolve(name)
            if resolved is not None and resolved.key in converting:
                wanted.add(special.redirect(path, resolved.key))
        body = _body_of_item(bodies, item)
        if body is not None:
            wanted |= {
                special.redirect(path, key) for _, key in body.requested if key in converting
            }

    for fixture in converting.values():
        source = layout.owning_file(fixture)
        if source is None:
            continue
        module = layout.home_module(source)
        wanted = consumers.setdefault(module, set())
        for edge in _resolved_at(ground_truth, fixture, fixture.visibility).values():
            if edge is not None and edge.key in converting:
                wanted.add(special.redirect(module, edge.key))
        body = _body_of(bodies, fixture)
        if body is not None:
            wanted |= {
                special.redirect(module, key) for _, key in body.requested if key in converting
            }

    for copy in special.copies.values():
        wanted = consumers.setdefault(copy.module, set())
        origin = ground_truth.fixture_defs[copy.origin]
        for edge in _resolved_at(ground_truth, origin, copy.node).values():
            if edge is not None and edge.key in converting:
                wanted.add(special.redirect(copy.module, edge.key))
        # A copy is the original's source, so it names everything the original's body named too.
        body = _body_of(bodies, origin)
        if body is not None:
            wanted |= {
                special.redirect(copy.module, key) for _, key in body.requested if key in converting
            }
    return consumers


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
    placed: Sequence[Declaration],
    special: Specialization,
    bodies: Mapping[tuple[str, str], Body],
    solo: Mapping[str, frozenset[str]],
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
                injections=_injections(
                    ground_truth, plan_layout, key, home.module, converting, special, bodies=bodies
                ),
                parametrized=fixture.is_parametrized,
            )
        )

    duplicates: dict[str, list[Duplicate]] = {}
    carried: dict[str, list[Import]] = {}
    needs: dict[str, set[str]] = {}
    for copy in sorted(special.copies.values(), key=lambda copy: (copy.module, copy.symbol)):
        injections = _injections(
            ground_truth,
            plan_layout,
            copy.origin,
            copy.module,
            converting,
            special,
            node=copy.node,
            bodies=bodies,
        )
        fixtures_by_file.setdefault(copy.host, []).append(
            FixtureWork(
                key=copy.key,
                argname=copy.symbol,
                symbol=copy.symbol,
                scope=SCOPES.get(copy.scope, "function"),
                injections=injections,
                parametrized=copy.parametrized,
            )
        )
        duplicates.setdefault(copy.host, []).append(
            Duplicate(
                symbol=copy.symbol,
                code=copy.code,
                after=tuple(injection.reference for injection in injections),
            )
        )
        carried.setdefault(copy.host, []).extend(copy.imports)
        needs.setdefault(copy.host, set()).update(copy.needs)

    tests_by_file: dict[str, list[TestWork]] = {}
    for (path, qualname), cases in sorted(items.items()):
        tests_by_file.setdefault(path, []).append(
            TestWork(
                qualname=qualname,
                injections=_test_injections(
                    ground_truth, cases[0], plan_layout, path, converting, special, bodies
                ),
            )
        )

    declares = {
        declaration.container: _references(plan_layout, declaration, special)
        for declaration in placed
    }
    marks = _marks(refusals, markers)
    paths = set(fixtures_by_file) | set(tests_by_file) | set(marks) | set(declares)
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
            imports=_imports_for(plan_layout, target, carried.get(path, ())),
            marks=tuple(sorted(marks.get(path, set()))),
            declares=declares.get(path, ()),
            duplicates=_ordered(duplicates.get(path, ())),
            needs=tuple(sorted(needs.get(path, set()))),
            solo=tuple(sorted(solo.get(path, ()))),
            context=_context(ground_truth, path, items, blocked_tests, bodies, special),
        )
    return work


def _ordered(duplicates: Sequence[Duplicate]) -> tuple[Duplicate, ...]:
    """`duplicates` with each copy behind the copies it names, which is the order they go in.

    A copy of a chain names the copy of the fixture below it, so writing them in the order the
    fixture graph keyed them would leave a `Depends()` reading a name nothing has bound.
    """
    remaining = list(duplicates)
    written: list[Duplicate] = []
    placed: set[str] = set()
    while remaining:
        pending = {copy.symbol for copy in remaining}
        ready = [copy for copy in remaining if not (pending & set(copy.after)) - placed]
        # A fixture graph is acyclic, so this only guards against writing nothing forever.
        ready = ready or remaining[:1]
        written.extend(ready)
        placed |= {copy.symbol for copy in ready}
        remaining = [copy for copy in remaining if copy not in ready]
    return tuple(written)


def _imports_for(plan_layout: Layout, target: str, carried: Sequence[Import]) -> tuple[Import, ...]:
    """The imports `target` needs, the wiring's own and the ones a copied body reads."""
    return tuple(dict.fromkeys([*plan_layout.imports_for(target), *carried]))


def _references(
    plan_layout: Layout, declaration: Declaration, special: Specialization
) -> tuple[str, ...]:
    """How the declaring module names each fixture it declares — its import, or its own binding.

    A container inside a subtree an override rules declares that subtree's copy, for the same
    reason a test inside it requests one: the object the container names is the object its tests
    get, and there are two of them.
    """
    found: list[str] = []
    for wanted in declaration.keys:
        key = special.redirect(declaration.container, wanted)
        home = plan_layout.home(key)
        if home is None:
            continue
        imported = plan_layout.importing(declaration.container, key)
        found.append(imported.bound if imported is not None else home.symbol)
    return tuple(found)


def _resolved_at(
    ground_truth: GroundTruth, fixture: FixtureDef, node: str
) -> Mapping[str, FixtureDef | None]:
    """The definition each parameter of `fixture`'s factory resolves to, asked from `node`.

    Resolution is per test in pytest, and an object is per node here: the copy of a fixture placed
    in an overriding directory reaches that directory's definitions, and the original reaches the
    ones visible where it is written. Asking the dump's chains from a node is what turns the one
    into the other, since a chain carries every definition of the name and nothing else decides
    which of them a consumer at a given depth gets.
    """
    chains = _chains_reaching(ground_truth, fixture.key, node)
    resolved: dict[str, FixtureDef | None] = {}
    for name in fixture.argnames:
        if name == REQUEST:
            continue
        chain = chains.get(name, ())
        if name == fixture.argname:
            # A fixture requesting its own name is asking for the definition it overrides.
            position = next((i for i, f in enumerate(chain) if f.key == fixture.key), 0)
            resolved[name] = chain[position - 1] if position else None
        else:
            resolved[name] = _visible_at(chain, node)
    return resolved


def _chains_reaching(
    ground_truth: GroundTruth, key: str, node: str
) -> Mapping[str, tuple[FixtureDef, ...]]:
    """The fixture chains of a test under `node` that reaches `key`, or of any test that does.

    A test outside the subtree has a shorter chain for an overridden name — it never saw the
    override — so a copy asks a test that did.
    """
    fallback: Mapping[str, tuple[FixtureDef, ...]] = {}
    for item in ground_truth.items:
        if not any(fixture.key == key for fixture in item.walk()):
            continue
        if audit_wiring.under(node, item.nodeid):
            return item.chains
        fallback = fallback or item.chains
    return fallback


def _visible_at(chain: Sequence[FixtureDef], node: str) -> FixtureDef | None:
    """The definition in `chain` that a consumer written at `node` gets: the nearest above it.

    A name defined only *below* `node` is one that only that subtree's tests can ask for, and the
    nearest definition is what every one of them resolves — so the chain's own winner answers it.
    """
    found = [fixture for fixture in chain if audit_wiring.under(fixture.visibility, node)]
    if found:
        return found[-1]
    return chain[-1] if chain else None


def _injections(
    ground_truth: GroundTruth,
    plan_layout: Layout,
    key: str,
    consumer: str,
    converting: Mapping[str, FixtureDef],
    special: Specialization,
    *,
    node: str | None = None,
    bodies: Mapping[tuple[str, str], Body] = {},
) -> tuple[Injection, ...]:
    """What each parameter of the fixture `key`'s factory becomes, seen from `node`."""
    fixture = ground_truth.fixture_defs[key]
    body = _body_of(bodies, fixture)
    resolved = dict(
        _resolved_at(ground_truth, fixture, node if node is not None else fixture.visibility)
    )
    names = _asked(ground_truth, fixture.argnames, resolved, body)
    found = _from_names(
        names,
        resolved,
        plan_layout,
        consumer,
        converting,
        fixture.is_parametrized,
        special,
        asked=_by_name(fixture.argnames, names),
    )
    return _without_request(found, fixture.argnames, body, parametrized=fixture.is_parametrized)


def _test_injections(
    ground_truth: GroundTruth,
    item: Item,
    plan_layout: Layout,
    consumer: str,
    converting: Mapping[str, FixtureDef],
    special: Specialization,
    bodies: Mapping[tuple[str, str], Body] = {},
) -> tuple[Injection, ...]:
    body = _body_of_item(bodies, item)
    resolved: dict[str, FixtureDef | None] = {name: item.resolve(name) for name in item.argnames}
    names = _asked(ground_truth, item.argnames, resolved, body)
    found = _from_names(
        names,
        resolved,
        plan_layout,
        consumer,
        converting,
        False,
        special,
        asked=_by_name(item.argnames, names),
    )
    return _without_request(found, item.argnames, body, parametrized=False)


def _asked(
    ground_truth: GroundTruth,
    argnames: tuple[str, ...],
    resolved: dict[str, FixtureDef | None],
    body: Body | None,
) -> tuple[str, ...]:
    """`argnames` plus the names this body asked for that are not among them, resolved alongside.

    A name asked for in source is a dependency the signature does not have yet, so the rewrite
    reads it exactly as it reads a parameter — and the parameter it grows takes its place.
    """
    names = list(argnames)
    for name, key in body.requested if body is not None else ():
        if name not in resolved:
            resolved[name] = ground_truth.fixture_defs.get(key)
        if name not in names:
            names.append(name)
    return tuple(names)


def _by_name(argnames: tuple[str, ...], names: tuple[str, ...]) -> frozenset[str]:
    """The names of `names` that no parameter declares, which are the ones a body asked for."""
    return frozenset(names) - frozenset(argnames)


def _without_request(
    injections: tuple[Injection, ...],
    argnames: tuple[str, ...],
    body: Body | None,
    *,
    parametrized: bool,
) -> tuple[Injection, ...]:
    """`injections`, plus the one that takes a spent `request` parameter away.

    A `request` a body reads only through the shapes this conversion rewrites is a parameter with
    nothing left to bind once they are rewritten, and velox has nothing to bind it to.
    """
    if body is None or parametrized or REQUEST not in argnames:
        return injections
    return (*injections, Injection(was=REQUEST, param="", reference=""))


def _from_names(
    names: tuple[str, ...],
    resolved: Mapping[str, FixtureDef | None],
    plan_layout: Layout,
    consumer: str,
    converting: Mapping[str, FixtureDef],
    parametrized: bool,
    special: Specialization,
    asked: frozenset[str] = frozenset(),
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
            # Inside the subtree an override rules, the object is that subtree's copy.
            key = special.redirect(consumer, fixture.key)
            home = plan_layout.home(key)
            if home is None:
                continue
            imported = plan_layout.importing(consumer, key)
            reference = imported.bound if imported is not None else home.symbol
            found.append(Injection(was=name, param=name, reference=reference, asked=name in asked))
            continue
        builtin = BUILTINS.get(fixture.argname)
        if builtin is not None:
            param, reference = builtin
            found.append(Injection(was=name, param=param, reference=reference, asked=name in asked))
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
    bodies: Mapping[tuple[str, str], Body] = {},
    special: Specialization = specialize.NONE,
) -> Context:
    tests = {qualname for (file, qualname) in items if file == path}
    blocked = {
        _qualname_of(item)
        for item in ground_truth.items
        if item.path == path and item.nodeid in blocked_tests
    }
    named = _bodies_in(bodies, path, ground_truth, special)
    return Context(
        path=path,
        axis_ids=_axis_ids(items, path),
        tests=frozenset(tests),
        blocked=frozenset(blocked),
        xfail_strict=_xfail_strict(ground_truth),
        requested={
            qualname: {name: _param_of(ground_truth, key, name) for name, key in body.requested}
            for qualname, body in named.items()
            if body.requested
        },
        finalizers=frozenset(qualname for qualname, body in named.items() if body.finalizers),
    )


def _bodies_in(
    bodies: Mapping[tuple[str, str], Body],
    path: str,
    ground_truth: GroundTruth,
    special: Specialization,
) -> Mapping[str, Body]:
    """The bodies this file's rewrite reads, by the name each is written under in it.

    A specialized copy is the duplicated source of a fixture written elsewhere, so what its body
    asked for is what the original's asked for — under the copy's own name, which is the name this
    file binds it to.
    """
    named = {qualname: body for (file, qualname), body in bodies.items() if file == path}
    for copy in special.copies.values():
        if copy.host != path:
            continue
        origin = ground_truth.fixture_defs.get(copy.origin)
        body = _body_of(bodies, origin) if origin is not None else None
        if body is not None:
            named[copy.symbol] = body
    return named


def _param_of(ground_truth: GroundTruth, key: str, name: str) -> str:
    """The parameter a requested name becomes, which is its own unless velox renames the fixture."""
    fixture = ground_truth.fixture_defs.get(key)
    builtin = BUILTINS.get(fixture.argname) if fixture is not None else None
    return builtin[0] if builtin is not None else name


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
