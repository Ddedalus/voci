"""What the dump says about the marks a suite's tests carry.

pytest resolves a mark's origin during collection — the test, its class, its module, or the ini
file — and velox has no such inheritance: a mark ends up on the test itself. So the questions here
are about reach and multiplicity rather than about spelling, which is what the dump answers
exactly.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

from velox_migrate import matrix
from velox_migrate.audit.findings import Finding, Site
from velox_migrate.audit.reach import Reach
from velox_migrate.model import GroundTruth, Item

# Marks velox records once per test, so two of them is a collection error rather than a contest.
SCALAR_MARKS = frozenset({"skip", "xfail", "timeout"})

# The session node, which is where a mark written in the ini file arrives from.
_SESSION = ""


def findings(ground_truth: GroundTruth, reach: Reach) -> list[Finding]:
    """Every mark finding the dump supports, in no particular order."""
    found: list[Finding] = []
    found += _usefixtures_findings(ground_truth, reach)
    found += _plugin_mark_findings(ground_truth)
    found += _duplicate_mark_findings(ground_truth)
    return found


def _usefixtures_findings(ground_truth: GroundTruth, reach: Reach) -> Iterator[Finding]:
    # Keyed by module and fixture name: `velox.use(...)` is declared per module, so whether a
    # `usefixtures` mark widens is decided by comparing against the module's other tests.
    covered: dict[tuple[str, str], set[str]] = {}
    origins: dict[tuple[str, str], str] = {}

    for item in ground_truth.items:
        if item.path is None:
            continue
        for name, origin in _usefixtures_with_origin(item):
            if origin == _SESSION:
                # Written in the ini file, so it already applies to every test.
                continue
            covered.setdefault((item.path, name), set()).add(item.nodeid)
            origins.setdefault((item.path, name), origin)

    for (path, name), tests in sorted(covered.items()):
        in_module = reach.tests_in_file(path)
        widens = len(tests) < len(in_module)
        yield Finding(
            code="VX010" if widens else "VX009",
            message=(
                f"`{name}` is declared for the whole module, reaching "
                f"{len(in_module) - len(tests)} test(s) that did not ask for it."
                if widens
                else f"`{name}` covers all {len(in_module)} test(s) in the module."
            ),
            site=_origin_site(origins[(path, name)], path),
            tests=in_module if widens else tuple(sorted(tests)),
            detail={"fixture": name, "requested_by": len(tests), "in_module": len(in_module)},
        )


def _plugin_mark_findings(ground_truth: GroundTruth) -> Iterator[Finding]:
    installed = {plugin.dist for plugin in ground_truth.plugins}
    grouped: dict[tuple[str | None, str], list[str]] = {}
    for item in ground_truth.items:
        for mark in item.markers_with_origin:
            dist = matrix.PLUGIN_MARKS.get(mark.name)
            if dist is not None and dist in installed:
                grouped.setdefault((item.path, mark.name), []).append(item.nodeid)

    for (path, name), tests in sorted(
        grouped.items(), key=lambda pair: (pair[0][0] or "", pair[0][1])
    ):
        dist = matrix.PLUGIN_MARKS[name]
        yield Finding(
            code="VX112",
            message=(
                f"`@pytest.mark.{name}` is acted on by {dist}, and becomes a tag that selects "
                f"the {len(tests)} test(s) carrying it without doing anything."
            ),
            site=Site(path),
            tests=tuple(sorted(tests)),
            detail={"mark": name, "plugin": dist},
        )


def _duplicate_mark_findings(ground_truth: GroundTruth) -> Iterator[Finding]:
    for item in ground_truth.items:
        counted: dict[str, list[str]] = {}
        for mark in item.markers_with_origin:
            if mark.name in SCALAR_MARKS:
                counted.setdefault(mark.name, []).append(mark.origin or _SESSION)
        for name, from_nodes in counted.items():
            if len(from_nodes) < 2:
                continue
            yield Finding(
                code="VX113",
                message=(
                    f"`{name}` reaches this test {len(from_nodes)} times, from "
                    f"{_listing(from_nodes)}."
                ),
                site=Site(item.path, item.lineno, _qualname(item)),
                tests=(item.nodeid,),
                detail={"mark": name, "origins": ", ".join(from_nodes)},
            )


def _usefixtures_with_origin(item: Item) -> Iterator[tuple[str, str]]:
    for mark in item.markers_with_origin:
        if mark.name != "usefixtures":
            continue
        for argument in mark.args:
            name = _literal(argument)
            if isinstance(name, str):
                yield name, mark.origin or _SESSION


def _literal(text: str) -> object:
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None


def _origin_site(origin: str, path: str) -> Site:
    """The mark's origin as a location: a module, a class in it, or one test."""
    if "::" not in origin:
        return Site(path)
    _, _, inner = origin.partition("::")
    return Site(path, None, inner.replace("::", "."))


def _listing(nodes: list[str]) -> str:
    return ", ".join(node or "the ini file" for node in nodes)


def _qualname(item: Item) -> str:
    return f"{item.cls}.{item.originalname}" if item.cls else item.originalname
