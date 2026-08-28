"""The audit: what this suite is made of, and what migrating it would cost.

Two sources of truth, joined here. The dump says what pytest resolved — every fixture a test
gets, every mark that reaches it, the configuration and the plugins — and the suite's own sources
say what is inside the bodies collection never looks at. Neither is guessed from the other: name
resolution comes only from the dump, and hazards come only from the sources.

Every classification is a support-matrix row, so an audit is a census keyed by code rather than a
prose opinion, and the numbers a reader acts on — what converts untouched, what needs a decision,
what fraction of the suite ends up running serially — are totals over those codes. Type readiness
is the one thing an audit reports that is not a row: it is a property of the suite's own
signatures rather than an occurrence of a pytest construct, so it hangs off `Audit` beside the
findings.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from velox_migrate.audit import config, marks, readiness, wiring
from velox_migrate.audit.findings import (
    Audit,
    Finding,
    Site,
    Suite,
    Summary,
    TypeReadiness,
    Unannotated,
    ordered,
    summarize,
)
from velox_migrate.audit.reach import Reach
from velox_migrate.audit.wiring import DEFAULT_BUDGET
from velox_migrate.model import GroundTruth

__all__ = [
    "DEFAULT_BUDGET",
    "Audit",
    "Finding",
    "Site",
    "Suite",
    "Summary",
    "TypeReadiness",
    "Unannotated",
    "run",
    "sources_of",
]


def run(ground_truth: GroundTruth, *, root: Path, budget: int = DEFAULT_BUDGET) -> Audit:
    """Audit the suite `ground_truth` describes, reading its sources from `root`.

    `root` is where the suite's files are now, which is the dump's rootdir when the audit runs
    where the extraction did. A source file the dump names and `root` does not hold is recorded as
    unread rather than skipped: the body-level counts are lower bounds while any file is missing.
    """
    # Deferred: reading a dump, and everything the dump alone answers, costs no LibCST.
    from velox_migrate.audit import sources

    reach = Reach(ground_truth)

    wiring_findings = wiring.findings(ground_truth, reach, budget=budget)
    mark_findings = marks.findings(ground_truth, reach)
    plugins_named = {
        str(finding.detail["plugin"]) for finding in wiring_findings if "plugin" in finding.detail
    }
    config_findings = config.findings(
        ground_truth, reach, root=root, reported_plugins=plugins_named
    )

    wanted = sources_of(ground_truth, root=root)
    present = [path for path in wanted if Path(root, path).is_file()]
    missing = [path for path in wanted if path not in set(present)]
    scan = sources.scan([Path(root, path) for path in present], root=root)
    # A construct that belongs to the suite — a hook, a plugin declaration — is charged to no
    # test: it converts nothing on its own, and counting it against every test in the file it sits
    # in would report a whole suite as unconvertible over one `conftest.py`.
    scanned = [
        finding
        if finding.construct.suite_level
        else finding.affecting(reach.tests_at(finding.site.file, finding.site.function))
        for finding in scan.findings
    ]

    findings = ordered([*wiring_findings, *mark_findings, *config_findings, *scanned])
    return Audit(
        suite=_suite(ground_truth, reach, findings, root=root, files=present),
        findings=findings,
        summary=summarize(findings, tests=len(ground_truth.items)),
        scanned_files=scan.files,
        unparsed=tuple(sorted({*scan.unparsed, *missing})),
        budget=budget,
        type_readiness=readiness.assess(ground_truth, root=root),
    )


def sources_of(ground_truth: GroundTruth, *, root: Path | None = None) -> tuple[str, ...]:
    """Every Python file of the suite worth reading, rootdir-relative and sorted.

    Test modules and the files that define fixtures, plus every `conftest.py` pytest loaded —
    including one that defines no fixture at all, since a conftest holding nothing but hooks is
    exactly the kind that does not convert. Given a `root`, their sibling modules are read too:
    a helper module beside a test file is where a suite keeps the code its tests call, and a
    hazard written there is one no test mentions.
    """
    wanted: set[str] = set()
    for item in ground_truth.items:
        if item.path is not None and item.path.endswith(".py"):
            wanted.add(item.path)
    for fixture in ground_truth.fixture_defs.values():
        if wiring.in_suite(fixture) and fixture.func.file is not None:
            wanted.add(fixture.func.file)
    for name in ground_truth.plugin_names:
        if name.endswith("conftest.py") and not name.startswith(("${", "/")):
            wanted.add(name)
    if root is not None:
        wanted |= _siblings(wanted, root)
    return tuple(sorted(wanted))


def _siblings(named: set[str], root: Path) -> set[str]:
    """Every Python module sitting in a directory the suite already has a file in."""
    found: set[str] = set()
    for directory in {str(PurePosixPath(path).parent) for path in named}:
        for path in sorted(Path(root, directory).glob("*.py")):
            if path.is_file():
                relative = PurePosixPath(directory) / path.name
                found.add(str(relative) if directory != "." else path.name)
    return found


def _suite(
    ground_truth: GroundTruth,
    reach: Reach,
    findings: Iterable[Finding],
    *,
    root: Path,
    files: list[str],
) -> Suite:
    reached = [
        fixture for fixture in wiring.reached(ground_truth).values() if not fixture.direct_param
    ]
    own = [fixture for fixture in reached if wiring.in_suite(fixture)]
    conftests = {
        fixture.func.file
        for fixture in own
        if fixture.func.file is not None and fixture.func.file.endswith("conftest.py")
    }
    findings = list(findings)
    return Suite(
        rootpath=ground_truth.rootpath,
        pytest_version=ground_truth.pytest_version,
        environment=dict(ground_truth.environment),
        tests=len(ground_truth.items),
        test_files=len({item.path for item in ground_truth.items if item.path is not None}),
        async_tests=_async_tests(ground_truth, root, files),
        fixtures=len(own),
        plugin_fixtures=len(reached) - len(own),
        conftests=len(conftests),
        overrides=sum(1 for finding in findings if finding.code in wiring.OVERRIDE_CODES),
        autouse_nodes=len(
            {finding.detail.get("node") for finding in findings if finding.code == "VX008"}
        ),
        plugins=tuple(sorted({plugin.dist for plugin in ground_truth.plugins})),
    )


def _async_tests(ground_truth: GroundTruth, root: Path, files: list[str]) -> int:
    """How many collected tests are `async def`.

    velox runs both kinds, so this is not a translation question — it is the denominator for the
    blocking-call hazard, which only bites inside a coroutine.
    """
    asynchronous: set[tuple[str, str]] = set()
    for file in files:
        try:
            tree = ast.parse(Path(root, file).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        for qualname, node in _functions(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                asynchronous.add((file, qualname))
    return sum(
        1
        for item in ground_truth.items
        if (item.path, f"{item.cls}.{item.originalname}" if item.cls else item.originalname)
        in asynchronous
    )


def _functions(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    found: list[tuple[str, ast.AST]] = []

    def walk(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((f"{prefix}{node.name}", node))
            elif isinstance(node, ast.ClassDef):
                walk(node.body, f"{prefix}{node.name}.")

    walk(tree.body, "")
    return found
