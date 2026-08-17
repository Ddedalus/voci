"""What the dump says about a suite's fixture wiring.

Collection already decided every question this module asks — which definition each test gets,
which fixtures an override sits above, where each autouse fixture applies — so nothing here
reasons about conftest scoping; it reads the answers and classifies them against the support
matrix.

Findings are raised for wiring that needs a reader's attention, plus the two mechanical shapes
whose site list *is* the deliverable: an override chain, because a specialized copy of it is
coming, and an autouse fixture, because that is where a `velox.use(...)` line lands.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

from velox_migrate import matrix
from velox_migrate.audit.findings import Finding, Site
from velox_migrate.audit.reach import Reach
from velox_migrate.model import FixtureDef, GroundTruth, Item

# The default fan-out budget: how many fixtures one override may cause to be duplicated before
# specializing the chain stops producing a reviewable diff.
DEFAULT_BUDGET = 5

# Constructs the source scan is the only honest witness for. A test that merely requests
# `monkeypatch` has changed nothing; a `monkeypatch.setenv` call has.
_SCANNED_ELSEWHERE = frozenset({"VX401"})

# pytest wraps a `setup_method` or a `TestCase` in a fixture of its own making. The class those
# belong to is a finding of its own, read from the class rather than from the fixture pytest
# invented for it.
_SYNTHETIC_PREFIXES = ("_xunit_", "_unittest_")


def findings(
    ground_truth: GroundTruth, reach: Reach, *, budget: int = DEFAULT_BUDGET
) -> list[Finding]:
    """Every wiring finding the dump supports, in no particular order."""
    found: list[Finding] = []
    found += _fixture_findings(ground_truth, reach)
    found += _override_findings(ground_truth, reach, budget=budget)
    found += _autouse_findings(ground_truth, reach)
    found += _indirect_findings(ground_truth)
    return found


def in_suite(fixture: FixtureDef) -> bool:
    """Whether this fixture is written in the suite rather than in pytest or an installed plugin."""
    file = fixture.func.file
    return file is not None and not file.startswith("${") and not file.startswith("/")


def reached(ground_truth: GroundTruth) -> dict[str, FixtureDef]:
    """The fixture definitions some collected test resolves, keyed as the dump keys them.

    The registry carries every fixture pytest knows about, its own builtins included, so a suite
    is described by what its tests reach rather than by what was installed alongside them.
    """
    return {fixture.key: fixture for item in ground_truth.items for fixture in item.walk()}


def _fixture_findings(ground_truth: GroundTruth, reach: Reach) -> Iterator[Finding]:
    provider = _providers(ground_truth)
    for fixture in reached(ground_truth).values():
        if fixture.direct_param or fixture.argname.startswith(_SYNTHETIC_PREFIXES):
            # The desugaring of `@parametrize`, or pytest's own wrapper around a class lifecycle:
            # neither is a fixture anyone wrote.
            continue
        if in_suite(fixture):
            yield from _suite_fixture(fixture, reach)
            continue
        yield from _foreign_fixture(fixture, provider, reach)


def _suite_fixture(fixture: FixtureDef, reach: Reach) -> Iterator[Finding]:
    if fixture.scope in ("class", "package"):
        wider = "module" if fixture.scope == "class" else "session"
        yield Finding(
            code="VX003",
            message=(
                f"`{fixture.argname}` is {fixture.scope}-scoped, and becomes {wider}-scoped, "
                f"which shares it with every test in the {wider}."
            ),
            site=_fixture_site(fixture),
            tests=reach.tests_of_fixture(fixture.key),
            detail={"fixture": fixture.argname, "scope": fixture.scope, "becomes": wider},
        )


def _foreign_fixture(
    fixture: FixtureDef, provider: dict[str, str], reach: Reach
) -> Iterator[Finding]:
    tests = reach.tests_of_fixture(fixture.key)
    module = fixture.func.module or ""
    root = module.split(".")[0]

    if root in ("_pytest", "pytest"):
        code = matrix.BUILTIN_FIXTURES.get(fixture.argname, "VX220")
        if code in _SCANNED_ELSEWHERE or matrix.construct(code).disposition is matrix.MECHANICAL:
            return
        yield Finding(
            code=code,
            message=f"`{fixture.argname}` is a pytest fixture {len(tests)} test(s) request.",
            tests=tests,
            detail={"fixture": fixture.argname},
        )
        return

    dist = provider.get(root, root.replace("_", "-"))
    recipe = matrix.plugin(dist)
    if recipe.construct.disposition is matrix.MECHANICAL:
        return
    code = "VX322" if recipe.code == "VX322" else "VX030"
    yield Finding(
        code=code,
        message=(
            f"`{fixture.argname}` comes from {dist}, which {len(tests)} test(s) depend on. "
            f"{recipe.note}"
        ),
        tests=tests,
        detail={"fixture": fixture.argname, "plugin": dist, "provider": module},
    )


def _override_findings(
    ground_truth: GroundTruth, reach: Reach, *, budget: int
) -> Iterator[Finding]:
    # Keyed by the overriding definition, since one override serves every test under its
    # directory and the fan-out is the union of what those tests reach through it.
    specialized: dict[str, set[str]] = {}
    overridden: dict[str, FixtureDef] = {}
    tests: dict[str, set[str]] = {}

    for item in ground_truth.items:
        for name, chain in item.chains.items():
            winner = chain[-1]
            if len(chain) < 2 or winner.direct_param or not in_suite(winner):
                continue
            if all(earlier.direct_param for earlier in chain[:-1]):
                continue
            overridden.setdefault(winner.key, chain[-2])
            tests.setdefault(winner.key, set()).add(item.nodeid)
            specialized.setdefault(winner.key, set()).update(_downstream(item, name, winner))

    for key, copies in sorted(specialized.items()):
        winner = ground_truth.fixture_defs[key]
        parent = overridden[key]
        fan_out = len(copies) + 1
        over_budget = fan_out > budget
        where = _node(winner.visibility)
        yield Finding(
            code="VX006" if over_budget else "VX005",
            message=(
                f"`{winner.argname}` in {where} overrides the one from "
                f"{_node(parent.visibility)}, so {fan_out} fixture(s) are copied for "
                f"{len(tests[key])} test(s)"
                + (f", past the budget of {budget}." if over_budget else ".")
            ),
            site=_fixture_site(winner),
            tests=tuple(sorted(tests[key])),
            detail={
                "fixture": winner.argname,
                "scope": where,
                "fan_out": fan_out,
                "budget": budget,
                "duplicated": ", ".join(
                    sorted(ground_truth.fixture_defs[copy].argname for copy in copies)
                ),
            },
        )


def _downstream(item: Item, name: str, winner: FixtureDef) -> set[str]:
    """The fixtures this test reaches that resolve `name` through `winner`, transitively.

    These are the copies a specialized chain needs: nothing about them changes except which
    definition of `name` they end up wired to, which in velox is a different object.
    """
    edges = {
        fixture.key: tuple(
            edge.fixture.key for edge in item.dependencies(fixture) if edge.fixture is not None
        )
        for fixture in item.walk()
    }
    found = {winner.key}
    changed = True
    while changed:
        changed = False
        for key, requested in edges.items():
            if key not in found and any(target in found for target in requested):
                found.add(key)
                changed = True
    return found - {winner.key}


def _autouse_findings(ground_truth: GroundTruth, reach: Reach) -> Iterator[Finding]:
    # Keyed by the node a fixture is visible from as well as its name: two directories may each
    # define an autouse fixture called `setup`, and the declaration for one goes in one of them.
    autouse = [
        fixture
        for fixture in ground_truth.fixture_defs.values()
        if fixture.autouse and in_suite(fixture)
    ]
    by_node = {(fixture.visibility, fixture.argname): fixture for fixture in autouse}
    for node, names in sorted(ground_truth.autouse_by_node.items()):
        covered = reach.tests_under(node)
        for name in names:
            fixture = by_node.get((node, name)) or _only(autouse, name)
            if fixture is None:
                # An autouse fixture from a plugin, already classified where it was defined.
                continue
            yield Finding(
                code="VX008",
                message=(
                    f"`{name}` runs for the {len(covered)} test(s) under {_node(node)}, and is "
                    "declared there."
                ),
                site=_fixture_site(fixture),
                tests=covered,
                detail={"fixture": name, "node": node},
            )


def _indirect_findings(ground_truth: GroundTruth) -> Iterator[Finding]:
    # One finding per parametrized name per test function, rather than per case: the mark is
    # written once and the generated fixtures are per value, not per case. The class counts too,
    # since two classes in one module may each have a `test_it`.
    seen: set[tuple[str | None, str | None, str, str]] = set()
    for item in ground_truth.items:
        for mark in item.markers_with_origin:
            if mark.name != "parametrize" or not mark.args:
                continue
            for argname in _parametrized_names(mark.args[0]):
                fixture = item.resolve(argname)
                if fixture is None or fixture.direct_param:
                    continue
                signature = (item.path, item.cls, item.originalname, argname)
                if signature in seen:
                    continue
                seen.add(signature)
                cases = tuple(
                    other.nodeid
                    for other in ground_truth.items
                    if (other.path, other.cls, other.originalname)
                    == (item.path, item.cls, item.originalname)
                )
                yield Finding(
                    code="VX007",
                    message=(
                        f"`{argname}` is parametrized indirectly, so each value it is given "
                        f"becomes its own fixture."
                    ),
                    site=Site(item.path, item.lineno, _qualname(item)),
                    tests=cases,
                    detail={"fixture": argname, "cases": len(cases)},
                )


def _parametrized_names(argnames: str) -> tuple[str, ...]:
    """The names one `parametrize` mark covers, read from the `repr` the dump carries.

    A mark's arguments survive as text, so the first one is a `repr` of either a comma-separated
    string or a sequence of names.
    """
    try:
        value = ast.literal_eval(argnames)
    except (ValueError, SyntaxError):
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(name) for name in value)
    return ()


def _providers(ground_truth: GroundTruth) -> dict[str, str]:
    """Top-level module name to the distribution that installed it, for every loaded plugin."""
    return {
        plugin.name.split(".")[0]: plugin.dist for plugin in ground_truth.plugins if plugin.name
    }


def _only(fixtures: list[FixtureDef], name: str) -> FixtureDef | None:
    """The one fixture called `name`, or `None` when the suite has none or several."""
    found = [fixture for fixture in fixtures if fixture.argname == name]
    return found[0] if len(found) == 1 else None


def _node(nodeid: str) -> str:
    """A visibility node as a reader would name it, the rootdir included."""
    return nodeid if nodeid not in ("", ".") else "the suite root"


def _fixture_site(fixture: FixtureDef) -> Site:
    return Site(fixture.func.file, fixture.func.lineno, fixture.func.qualname)


def _qualname(item: Item) -> str:
    return f"{item.cls}.{item.originalname}" if item.cls else item.originalname
