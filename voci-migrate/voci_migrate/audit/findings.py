"""What an audit produces: a finding per construct occurrence, and the totals over them.

A `Finding` is a support-matrix code plus where it was seen, and it borrows everything else —
what the construct is, what becomes of it, whether it costs concurrency — from the row that code
names. Nothing here decides policy; the matrix does, so a report and a rewrite rule cannot
disagree about a construct's classification.

The totals answer the adoption question the audit exists to answer: how much of this suite
converts untouched, how much needs a human, and what fraction of it ends up running serially.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from voci_migrate import matrix
from voci_migrate.matrix import Area, Construct, Disposition

# Worst-first. A test's own classification is the worst of its findings', and a report leads with
# the same order.
SEVERITY: tuple[Disposition, ...] = (
    Disposition.UNSUPPORTED,
    Disposition.REFUSED,
    Disposition.HAZARD,
    Disposition.MARKER,
    Disposition.MECHANICAL,
)

_RANK = {disposition: rank for rank, disposition in enumerate(SEVERITY)}


@dataclass(frozen=True, slots=True)
class Site:
    """Where a construct was seen.

    `file` is rootdir-relative and `line` counts from one. `function` is the qualified name of the
    enclosing test, method or fixture factory, and is `None` for a construct at module level or
    for one read from the dump rather than from source.
    """

    file: str | None = None
    line: int | None = None
    function: str | None = None

    def __str__(self) -> str:
        if self.file is None:
            return "<the suite>"
        where = self.file if self.line is None else f"{self.file}:{self.line}"
        return where if self.function is None else f"{where} ({self.function})"

    @property
    def sort_key(self) -> tuple[str, int, str]:
        return (self.file or "", self.line or 0, self.function or "")


@dataclass(frozen=True, slots=True)
class Finding:
    """One occurrence of a support-matrix construct in this suite.

    `message` is what is true of this occurrence — the rest is the construct's, read through
    `construct`. `tests` are the node ids the occurrence affects, which is how a hazard in a
    fixture body reaches the tests that never mention it; it is empty for a finding about the
    suite as a whole. `detail` carries the numbers a report quotes, such as an override's fan-out.
    Constructing a `Finding` with a code the matrix does not define raises `KeyError`.
    """

    code: str
    message: str
    site: Site = Site()
    tests: tuple[str, ...] = ()
    detail: Mapping[str, str | int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        matrix.construct(self.code)

    @property
    def construct(self) -> Construct:
        return matrix.construct(self.code)

    @property
    def disposition(self) -> Disposition:
        return self.construct.disposition

    @property
    def area(self) -> Area:
        return self.construct.area

    @property
    def subject(self) -> str:
        return self.construct.subject

    @property
    def marker(self) -> str | None:
        return self.construct.marker

    @property
    def serialized(self) -> bool:
        return self.construct.serialized

    @property
    def sort_key(self) -> tuple[int, str, tuple[str, int, str], str]:
        return (_RANK[self.disposition], self.code, self.site.sort_key, self.message)

    def affecting(self, tests: Iterable[str]) -> Finding:
        """This finding, recording the tests it affects."""
        return Finding(
            code=self.code,
            message=self.message,
            site=self.site,
            tests=tuple(sorted(set(tests))),
            detail=self.detail,
        )


@dataclass(frozen=True, slots=True)
class Unclassified:
    """A fixture, test or class the dump says pytest resolved, whose defining code no scan located.

    Not a `Finding`: there is no matrix code for it, because the whole point is that no rule ever
    ran against it. `kind` is `"fixture"`, `"test"` or `"class"`. `tests` are the node ids it
    reaches, by the same attribution `Finding.tests` uses, and are excluded from
    `Summary.clean_tests` rather than counted as a construct nothing found wrong with.
    """

    kind: str
    name: str
    site: Site
    tests: tuple[str, ...] = ()

    @property
    def sort_key(self) -> tuple[str, tuple[str, int, str], str]:
        return (self.kind, self.site.sort_key, self.name)


@dataclass(frozen=True, slots=True)
class Summary:
    """The totals a reader needs before deciding whether to convert anything.

    Every test falls in exactly one of `clean_tests`, `marker_tests`, `hazard_tests` and
    `blocked_tests`, by the worst finding that touches it; a test nothing touches, and that no
    `Unclassified` entry reaches either, is clean. `serialized_tests` cuts across all four, since a
    construct can be mechanical to translate and still have to run alone.
    """

    tests: int
    findings: int
    by_disposition: Mapping[Disposition, int]
    by_code: Mapping[str, int]
    clean_tests: int
    marker_tests: int
    hazard_tests: int
    blocked_tests: int
    serialized_tests: int
    suite_findings: int
    unclassified_tests: int = 0

    @property
    def convertible_tests(self) -> int:
        """Tests conversion produces working code for, review markers included."""
        return self.tests - self.blocked_tests

    @property
    def serialized_percent(self) -> float:
        return _percent(self.serialized_tests, self.tests)

    @property
    def clean_percent(self) -> float:
        return _percent(self.clean_tests, self.tests)

    @property
    def blocked_percent(self) -> float:
        return _percent(self.blocked_tests, self.tests)


@dataclass(frozen=True, slots=True)
class Unannotated:
    """One fixture factory written without a return annotation.

    `injections` counts the parameters that receive it — one per test function and one per fixture
    factory that requests it, however many cases a test was parametrized into.
    """

    argname: str
    module: str | None
    site: Site
    injections: int

    @property
    def sort_key(self) -> tuple[int, tuple[str, int, str], str]:
        return (-self.injections, self.site.sort_key, self.argname)


@dataclass(frozen=True, slots=True)
class TypeReadiness:
    """What the suite's fixtures state about their own types, and what conversion can carry.

    This is not a support-matrix code, and adding one would say something untrue: a missing return
    annotation is no pytest construct, occurs nowhere in particular, and blocks nothing. The
    fixture converts, and every parameter it is injected into loses its type. It is an axis
    beside the census rather than a row in it, which is why it hangs off the audit on its own.

    `fixtures` is ordered by the cost of leaving each one as it is, so it reads as a worklist.
    """

    fixtures: tuple[Unannotated, ...] = ()
    annotated: int = 0

    @property
    def total(self) -> int:
        """Every fixture the suite wrote, annotated or not."""
        return self.annotated + len(self.fixtures)

    @property
    def injections(self) -> int:
        """The parameters that lose their type while nothing here is annotated."""
        return sum(row.injections for row in self.fixtures)


@dataclass(frozen=True, slots=True)
class Suite:
    """The shape of the suite the audit read, as the dump reports it."""

    rootpath: str
    pytest_version: str
    environment: Mapping[str, str]
    tests: int
    test_files: int
    async_tests: int
    fixtures: int
    plugin_fixtures: int
    conftests: int
    overrides: int
    autouse_nodes: int
    plugins: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Audit:
    """Everything one audit of a suite concluded.

    `unparsed` names the source files the static scan could not read; while it is non-empty the
    body-level counts are lower bounds, which every rendering of an audit has to say. `unclassified`
    is the same kind of gap at the level of one fixture, test or class rather than a whole file: its
    defining code parsed fine but no scan located it there, so unlike a `Finding` it names no matrix
    code — only that nothing looked.
    """

    suite: Suite
    findings: tuple[Finding, ...]
    summary: Summary
    scanned_files: int
    unparsed: tuple[str, ...]
    budget: int
    type_readiness: TypeReadiness = field(default_factory=TypeReadiness)
    unclassified: tuple[Unclassified, ...] = ()

    def of_disposition(self, disposition: Disposition) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.disposition is disposition)

    def of_area(self, area: Area) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.area is area)

    @property
    def blind_spots(self) -> tuple[Construct, ...]:
        """The constructs no audit can see, which its numbers therefore exclude."""
        return tuple(c for c in matrix.CONSTRUCTS if not c.detected)


def summarize(
    findings: Sequence[Finding], *, tests: int, unclassified: Collection[Unclassified] = ()
) -> Summary:
    """The totals over `findings` for a suite of `tests` collected tests.

    `unclassified` pulls the tests it reaches out of `clean_tests` on top of `findings`' own worst-
    finding tally, since a test only one of those touches would otherwise read as clean either way.
    """
    by_disposition = {disposition: 0 for disposition in SEVERITY}
    by_code: dict[str, int] = {}
    worst: dict[str, Disposition] = {}
    serialized: set[str] = set()
    suite_findings = 0

    for finding in findings:
        by_disposition[finding.disposition] += 1
        by_code[finding.code] = by_code.get(finding.code, 0) + 1
        if not finding.tests:
            suite_findings += 1
        for nodeid in finding.tests:
            if _RANK[finding.disposition] < _RANK.get(
                worst.get(nodeid, Disposition.MECHANICAL), 99
            ):
                worst[nodeid] = finding.disposition
            if finding.serialized:
                serialized.add(nodeid)

    counted = {disposition: 0 for disposition in SEVERITY}
    for disposition in worst.values():
        counted[disposition] += 1
    blocked = counted[Disposition.UNSUPPORTED] + counted[Disposition.REFUSED]
    unclassified_tests = {nodeid for row in unclassified for nodeid in row.tests} - worst.keys()

    return Summary(
        tests=tests,
        findings=len(findings),
        by_disposition=by_disposition,
        by_code={code: by_code[code] for code in sorted(by_code)},
        # Everything not touched by a finding worse than mechanical and not reached by an
        # unclassified fixture, test or class either, which is why this is a remainder rather than
        # its own tally.
        clean_tests=tests
        - blocked
        - counted[Disposition.HAZARD]
        - counted[Disposition.MARKER]
        - len(unclassified_tests),
        marker_tests=counted[Disposition.MARKER],
        hazard_tests=counted[Disposition.HAZARD],
        unclassified_tests=len(unclassified_tests),
        blocked_tests=blocked,
        serialized_tests=len(serialized),
        suite_findings=suite_findings,
    )


def ordered(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    """`findings` worst-first, then by code and location, so two audits of a suite read alike."""
    return tuple(sorted(findings, key=lambda finding: finding.sort_key))


def unclassified_ordered(rows: Iterable[Unclassified]) -> tuple[Unclassified, ...]:
    """`rows` by kind, then by location, so two audits of a suite read alike."""
    return tuple(sorted(rows, key=lambda row: row.sort_key))


def _percent(part: int, whole: int) -> float:
    return 0.0 if whole == 0 else round(100.0 * part / whole, 1)
