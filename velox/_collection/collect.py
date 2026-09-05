"""Collection: import test modules and build the flat list of test records.

`TestRecord` carries what the runner needs to execute a test: `id`, `path`, `lineno`, `qualname`,
`func`, its `params` (if `@velox.parametrize`d), its resolved `plan`, and its `marks`, whose
conditions are decided here so nothing downstream evaluates a suite's own expression again. A test
marked `@velox.skip`/`@velox.skipif` is read off the function object and excluded from `records`
into `skipped` instead of running for real; a `tag_expr` (see `selection.py`) whose expression a
test's `@velox.tag(...)` names don't satisfy excludes it into `deselected` instead -- checked
after the skip check, so a skip-marked test is always `skipped`, never reclassified as deselected
depending on `-m`. `keyword_expr` (`-k`) and `id_selection` (`path.py::test_name` arguments,
`targets.py`) deselect the same way, but against a whole test id, so both see each
`@velox.parametrize` case's own `[case]` suffix. A `Depends(...)`-defaulted parameter is resolved
via `_fixtures.plan_for`, alongside the fixtures the module declared for all of its tests with
`velox.use(...)` (`requires.py`), and a malformed DI graph (bad scope nesting, a missing
injection) becomes a `CollectionError`, the same way a bad import does. The declarations reaching
one test are those of its own module together with those of every package above it, outermost
first (`requires.package_inits`).

A `@velox.parametrize`d test expands into one record per case
(`parametrize.cases_for`), sharing the one `ResolutionPlan` built for the function — parametrize
values are call kwargs, not part of the DI graph. A case written as `velox.case(value, marks=...)`
carries marks of its own, folded into the function's on that one record (`_case_disposition`), so
one case can be skipped, tagged or expected to fail while its siblings run. A test transitively
depending on a fixture built with `params=` expands the same way, on the DI graph instead
(`_fixtures.expand_cases`); the two axes cross freely, so both together multiply.

A decorated test is read through its decorators: its injection plan and its definition line come
from the function underneath (`_mocking.real_function`), while the decorated object is what runs.
`unittest.mock` patching found on the way down (`_mocking.patching_of`) lands on `TestRecord` as
`patches`, which is what schedules the test alone.

Import mechanics: importlib only, path-derived module names under `velox_tests.*`, one entry per
module never touching `sys.path`, an exception during `exec_module` becomes a `CollectionError`
attributed to that file rather than aborting the run. The assertion-rewriting meta-path hook, when
installed, is consulted explicitly (`_import_module`) — `spec_from_file_location` alone never
gives it the chance to run. The packages above a test file go through the same import, ahead of
the file itself.

Both `async def test_*` and plain `def test_*` functions are collected; `_run.py` runs the sync
ones on the context-propagating executor rather than inline. A `class Test*` is pure namespacing:
its `test_*` methods are collected as `path.py::TestGroup::test_name`, each called on a fresh
instance constructed for that one test, so nothing is shared through `self`.

A group collects the `test_*` methods it inherits as well as its own (`_test_methods`), so a
shared base of tests runs once per group that inherits it.

Shapes that would otherwise contribute nothing, silently, are `CollectionError`s naming what to
do instead (`_shape_problem`, `_class_problems`): a class velox can't construct or whose
`setup_method`-style hooks would never run, a `unittest.TestCase`, a `test_*` method on a class
named like a test suite but not `Test*` and inherited by no group, a `test_*` name bound to a
function defined under a different name, and a `test_*` function that yields instead of running.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import re
import sys
import traceback
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from velox._assertions import rewrite as _rewrite
from velox._collection.parametrize import Case, cases_for, known_params_of
from velox._collection.requires import REQUIRES_ATTR, combined, package_inits, requires_of
from velox._collection.selection import KeywordExpression, TagExpression
from velox._collection.targets import IdSelection
from velox._di.fixtures import Fixture, ResolutionPlan, expand_cases, plan_for
from velox._marks import MARKS_ATTR, NO_MARKS, Marks, decided, holds, marks_of, merged
from velox._mocking import patching_of, real_function

__all__ = [
    "CollectionError",
    "CollectionResult",
    "Skipped",
    "TestRecord",
    "collect",
    "display_path",
    "module_name_for",
]

#: Everything under this prefix is a velox-imported test module. Never `sys.path`-relative — the
#: whole point is that two `test_utils.py` in different directories get different dotted names
#: instead of colliding.
_MODULE_PREFIX = "velox_tests"

#: What survives unescaped in a dotted module segment.
_NON_IDENTIFIER_CHARS = re.compile(r"[^0-9A-Za-z_]")


@dataclass(frozen=True, slots=True)
class TestRecord:
    """One collected test, ready to run."""

    id: str
    index: int
    path: Path
    lineno: int
    qualname: str
    func: Callable[..., object]
    params: Mapping[str, object] | None
    """This case's `@velox.parametrize` values, keyed by argument name — passed to `func` as
    extra kwargs alongside `plan`'s injected ones. `None` for a test with no `parametrize` mark;
    an empty dict never occurs (`@velox.parametrize` rejects an empty `argnames`)."""
    plan: ResolutionPlan
    """This test function's whole transitive fixture graph, already resolved. Every record
    carries one, even a test with no `Depends(...)` at all (`steps=()`, `root_args=()`) —
    uniformity here is what keeps the runner a single code path instead of an "if it has
    fixtures" branch. Identical (the same object) across every `@velox.parametrize` case of the
    same function, since parametrize values are call kwargs, not part of the DI graph — but
    distinct per fixture-case combination for a function depending on a `params=` fixture, since
    that's what specializes each fixture's construction and cache key to its chosen case."""
    marks: Marks
    """This record's marks: the function's own, with the marks of its `velox.case(...)` cases
    folded in, and every condition already decided -- a `@velox.xfail(condition=...)` that does
    not hold is not here at all. The runner reads its marks from this rather than from `func`,
    since two cases of one function need not carry the same ones."""
    patches: tuple[str, ...] = ()
    """What this test patches with `unittest.mock`, one display name per patcher
    (`_mocking.patching_of`). Non-empty means the test installs a process-global override and
    is scheduled to run alone."""


@dataclass(frozen=True, slots=True)
class CollectionError:
    """An import failure attributed to one file, a test whose dependency graph is malformed
    (bad scope nesting, a missing injection), or a test shape velox refuses to collect
    silently (see `_shape_problem` and `_class_problems`)."""

    path: Path
    message: str


@dataclass(frozen=True, slots=True)
class Skipped:
    """A collected test excluded from `records` because a skip mark said so — contrast
    `CollectionError`, which means something is *wrong*; this means the suite asked, correctly,
    for the test not to run.
    """

    id: str
    reason: str
    path: Path
    """The file this test was collected from, relative to `rootdir` exactly as `TestRecord.path`
    is, so the reporter can count a skip against the same file block as its siblings that ran."""


@dataclass(frozen=True, slots=True)
class CollectionResult:
    records: list[TestRecord]
    errors: list[CollectionError]
    skipped: list[Skipped]
    deselected: list[str] = field(default_factory=list)
    """Ids of tests excluded by `tag_expr`, `keyword_expr` or `id_selection`.

    A `skip`-marked test lands here when `keyword_expr` or `id_selection` leaves it out of the
    run: what the run is about is settled before what each test would have done. Everything else
    here would have run.
    """
    unexpanded: list[str] = field(default_factory=list)
    """The ids in `skipped` and `deselected` that name a whole test rather than one case of it.

    A test excluded before its `@velox.parametrize` cases are built has no per-case ids to be
    listed under, and `velox.skip` plus `tag_expr` both exclude that early -- deliberately, since
    building the cases resolves a DI graph neither has any use for. Callers matching a
    `test_name[case]` selector against these ids need to know they stop short of the `[case]`
    part (`IdSelection.unmatched`); nothing else does.
    """


def module_name_for(path: Path, rootdir: Path) -> str:
    """`velox_tests.<dotted.relpath.without.suffix>`.

    Path-derived so two `test_utils.py` files in different directories never collide — the
    entire content of pytest's `ImportPathMismatchError`, deleted rather than solved.
    Non-identifier characters in path segments are escaped (`_escape_segment`) so the result is
    always a legal dotted module name; a segment that needed escaping also gets a short digest
    of its original text appended, because the escape alone is lossy (`api-v2` and `api_v2`
    would otherwise both become `api_v2`) and would silently reopen the exact collision this
    function exists to prevent.
    """
    path = Path(path).resolve()
    rootdir = Path(rootdir).resolve()
    try:
        rel = path.relative_to(rootdir)
    except ValueError:
        # `path` isn't under `rootdir` at all — an explicit file argument elsewhere on disk,
        # say. Anchor on the absolute path's own segments (dropping the root/drive) instead of
        # raising: it's still deterministic and still collision-free, just not rootdir-relative.
        rel = Path(*path.parts[1:]) if path.is_absolute() else path

    *dirs, filename = rel.parts
    segments = [_escape_segment(part) for part in (*dirs, Path(filename).stem)]
    return ".".join([_MODULE_PREFIX, *segments])


def _escape_segment(segment: str) -> str:
    """One dotted-name component: non-identifier characters replaced, leading digit guarded."""
    escaped = _NON_IDENTIFIER_CHARS.sub("_", segment)
    if escaped != segment:
        # Escaping changed something, so it's lossy for this segment specifically — append a
        # short digest of the *original* text to keep differently-spelled segments that collapse
        # to the same escaped form apart. Segments that needed no escaping are left exactly
        # alone (no digest), which is what keeps the common case's module names readable.
        digest = hashlib.blake2b(segment.encode(), digest_size=3).hexdigest()
        escaped = f"{escaped}_{digest}"
    if not escaped or escaped[0].isdigit():
        escaped = f"_{escaped}"
    return escaped


def display_path(path: Path, resolved_rootdir: Path) -> Path:
    """`path`, relative to `rootdir` when possible.

    `TestRecord.path` is relative to `rootdir` so a test id stays portable across machines,
    instead of baking in an absolute path. Falls back to the resolved absolute path for a file
    outside `rootdir` (an explicit argument elsewhere on disk), the same case `module_name_for`
    falls back on.
    """
    resolved = path.resolve()
    try:
        return resolved.relative_to(resolved_rootdir)
    except ValueError:
        return resolved


def _skip_reason(marks: Marks) -> str | None:
    """The reason this test should not run, or `None`. `skip` always wins; among `skipifs`
    (which legitimately stack), the first truthy condition's reason is used."""
    if marks.skip is not None:
        return marks.skip.reason
    for skipif in marks.skipifs:
        if holds(skipif.condition):
            return skipif.reason
    return None


def _case_disposition(base: Marks, case: Case | None) -> tuple[Marks, str | None]:
    """One case's effective marks -- `base`, with `case`'s own folded in -- and the reason that
    case does not run, or `None`.

    `base` arrives already decided: a test whose `skip`/`skipifs` resolve to "runs" only reaches
    here after `collect` confirmed that, and its `xfail` condition was decided at the same time.
    Only `case`'s own marks are new; re-deciding `base`'s again would evaluate a `skipif` or
    `xfail` condition a second time, which callers of `decided` are promised never happens.
    """
    if case is None or case.marks == NO_MARKS:
        return base, None
    case_marks = merged(base, case.marks)
    if case.marks.xfail is not None:
        case_marks = decided(case_marks)
    return case_marks, _skip_reason(case.marks)


def _cases_carry_tags(marks: Marks) -> bool:
    """Whether any `velox.case(...)` in this test's parametrizations carries a tag.

    Where one does, `-m` cannot be answered for the whole function -- one case may be tagged
    `slow` and another not -- so the tag check waits for the expansion (`collect`).
    """
    return any(
        case_marks.tags
        for param_set in marks.parametrizations
        for case_marks in param_set.case_marks
    )


def collect(
    files: Iterable[Path],
    *,
    rootdir: Path,
    tag_expr: TagExpression | None = None,
    keyword_expr: KeywordExpression | None = None,
    id_selection: IdSelection | None = None,
    collectible: Iterable[Path] = (),
) -> CollectionResult:
    """Import each file and build its records; `index` assigned once over the whole result.

    `collectible` is the wider set of test files discovery found, when `files` is a narrowed
    slice of it (`--lf`). Nothing in it is imported; it only tells `_misplaced_declarations`
    which modules are test modules, so narrowing the run cannot invent a collection error.

    Per file, in the order given:

    1. Read what the packages above the file declared with `velox.use(...)`
       (`_package_declarations`), importing each `__init__.py` at most once per call. An
       `__init__.py` that fails to import is a `CollectionError` of its own, and every file
       underneath it is skipped rather than run without the fixtures its package declared.
    2. Compute the module name (`module_name_for`) and import it (`_import_module`, which
       consults the installed assertion-rewriting hook first). An exception here becomes a
       `CollectionError`; the file contributes zero records and collection continues to the next
       file.
    3. Within the imported module, find the tests *defined* in it (`_module_candidates`) — i.e.
       `getattr(obj, "__module__", None) == module.__name__`, so a `test_*` helper imported from
       elsewhere isn't collected twice, nor given the importing module's `velox.use(...)`
       declarations. That is every module-level `test_*` function plus the `test_*` methods of
       every `class Test*`, which velox treats as pure namespacing. A shape that would collect
       as nothing at all is a `CollectionError` instead, one per offending class or name.
    4. Sort by definition line (`_definition_line`, read through any decorators) — source order,
       whether a test is a module-level function or a method, not `vars()` iteration order.
    5. Per test: a `skip`/truthy-`skipif` mark on the function excludes it from `records` into
       `skipped`
       instead, unless `keyword_expr` or `id_selection` leaves it out of the run altogether, in
       which case it goes to `deselected` — both are matched against its bare id, its cases
       never having been worked out. Otherwise `tag_expr`, if given, excludes a test whose
       `@velox.tag(...)` names don't satisfy it into `deselected`, and a skip outranks it: a
       skipped test is skipped for the reason it gives whatever tags are in play. Otherwise a
       malformed DI graph — its own, or one the
       `velox.use(...)` fixtures reaching it introduce — or a name collision between stacked
       `@parametrize`s, excludes it into `errors` instead. Otherwise build one
       `TestRecord` per case in the cartesian product of `@velox.parametrize`'s cases and the
       fixture graph's own parametrized-fixture cases (one of each, for a function using
       neither), `id` as `"{path}::{name}"`, or `"{path}::{name}[{case_id}]"` when either
       axis contributes, with `path` relative to `rootdir`, `name` the test's `::`-joined
       class path and function name, and each record carrying the `ResolutionPlan` specialized
       for its own fixture-case combination. `keyword_expr` and `id_selection` are applied last,
       to each of those ids, so a `-k` term or a `path.py::test_name[case]` argument matching one
       case of a parametrized test selects that case alone; the rest go to `deselected`. A
       `velox.case(..., marks=...)` case is skipped or deselected there too, on its own merged
       marks -- and where any case carries a tag of its own, `tag_expr` is answered there rather
       than above, the function's tags being no answer for cases that differ.

    The `velox.use(...)` declarations reaching a file are validated once, before its tests: a
    malformed declared graph is one `CollectionError` for the file, which then contributes no
    records. Once every file is done, a `velox.use(...)` call left on a module that is neither a
    collected test file nor a package velox read declarations from becomes a `CollectionError` of
    its own (`_misplaced_declarations`).

    `files` is assumed already de-duplicated and in deterministic order (`discover_files` gives
    you both); `index` is assigned across the concatenation of all files' records, in that order
    -- deselected tests never consume an index. A test `keyword_expr`/`id_selection` deselects
    still has its DI graph resolved on the way there (its `[case]` ids don't exist before
    expansion), so a malformed graph is reported whether or not `-k` would have run it.
    """
    records: list[TestRecord] = []
    errors: list[CollectionError] = []
    skipped: list[Skipped] = []
    deselected: list[str] = []
    unexpanded: list[str] = []
    index = 0
    resolved_rootdir = Path(rootdir).resolve()

    #: Seeded with every file discovery found -- `collectible`, which `--lf` narrows `files`
    #: down from -- rather than only the ones this call collects. A `velox.use(...)` in a test
    #: module left out of this run is a declaration in a test module all the same, and
    #: `_misplaced_declarations` must not report it as misplaced because a file that *was*
    #: collected imported that module under its real name.
    declaring_files: set[Path] = set()
    for collectible_path in collectible:
        declaring_files.add(Path(collectible_path).resolve())
        declaring_files.update(package_inits(collectible_path, rootdir))
    #: `__init__.py` path -> what that package declared, or `None` if it failed to import. One
    #: entry per package for the whole call, so every test under a package shares the one import
    #: and therefore the one `Fixture` object: two imports would be two identities, and a
    #: `scope="session"` declared fixture would then build once per importing subtree.
    package_declarations: dict[Path, tuple[Fixture[Any], ...] | None] = {}

    for path in files:
        resolved_path = Path(path).resolve()
        declaring_files.add(resolved_path)
        relpath = display_path(path, resolved_rootdir)

        inherited = _package_declarations(
            path, rootdir=rootdir, cache=package_declarations, errors=errors
        )
        if inherited is None:
            # A package above this file failed to import; the error naming it is already
            # recorded. Running its tests anyway would silently drop declared setup.
            continue

        module_name = module_name_for(path, rootdir)
        try:
            module = _import_module(path, module_name)
        except Exception as error:
            # Attributed to the file, not raised: one broken test module must not take the
            # rest of the suite down with it.
            errors.append(CollectionError(path=relpath, message=_import_failure(error, path)))
            continue

        implicit = combined(inherited, requires_of(module))
        if implicit:
            try:
                # Validated once against a stand-in body rather than per test: the declared graph
                # is the same for all of them, so the alternative is one identical traceback per
                # test in the file.
                plan_for(_no_dependencies, implicit=implicit)
            except Exception:
                errors.append(CollectionError(path=relpath, message=traceback.format_exc()))
                continue

        candidates, problems = _module_candidates(module, module_name)
        errors.extend(CollectionError(path=relpath, message=problem) for problem in problems)

        for candidate in candidates:
            func = candidate.func
            test_id = f"{relpath}::{candidate.name}"
            marks = marks_of(func)
            try:
                reason = _skip_reason(marks)
            except Exception:
                # One test's malformed marks must not abort the file's remaining tests any more
                # than a broken import aborts the remaining files.
                errors.append(CollectionError(path=relpath, message=traceback.format_exc()))
                continue

            if reason is not None:
                # Every exit from here on names the test by its bare id, cases and all: what
                # follows excludes it before expansion, which is what would have built them.
                unexpanded.append(test_id)
                # -k and a `path.py::test_name` argument say which tests this run is about at
                # all, so a skip they exclude is not its business to report. They differ in
                # reach here, and only here: `test_role[admin]` names this exact test whatever
                # its cases turn out to be, while a -k term is matched against the id that
                # exists -- so -k admin, which would have found the case, doesn't find this.
                if keyword_expr is not None and not keyword_expr.matches(test_id):
                    deselected.append(test_id)
                    continue
                if id_selection is not None and not id_selection.selects_unexpanded(
                    resolved_path, candidate.name
                ):
                    deselected.append(test_id)
                    continue
                # Ahead of tag_expr: a test marked skip is skipped for the reason it gives,
                # regardless of -m -- @velox.skip is never silently reclassified as deselected
                # depending on which tags happen to be in play.
                skipped.append(Skipped(id=test_id, reason=reason, path=relpath))
                continue

            # Deferred to the expansion below when a case carries tags of its own: the
            # function's tags are then not the whole answer for any of its cases.
            per_case_tags = _cases_carry_tags(marks)
            if tag_expr is not None and not per_case_tags and not tag_expr.matches(marks.tags):
                deselected.append(test_id)
                unexpanded.append(test_id)
                continue

            # A decorator's wrapper takes `(*args, **kwargs)`, so the injection plan and the
            # definition line are read off the function underneath it; `func` itself stays what
            # gets called, since the decorator is the whole point of applying it.
            defined = real_function(func)
            patching = patching_of(func)
            try:
                # `known_params` must be computed before `plan_for`, which reads it to keep a
                # parametrized argument from reading as a missing injection -- and `cases_for`
                # only needs to run after, to build this same function's expanded call kwargs.
                # A `mock.patch.multiple` parameter is supplied by name at call time exactly as
                # a parametrized one is, so it joins the same set.
                known_params = known_params_of(marks.parametrizations) | patching.keyword_args
                plan = plan_for(
                    defined,
                    known_params=known_params,
                    implicit=implicit,
                    # A method's `self`/`cls` is supplied by the call `_receiving` builds around
                    # it, ahead of whatever mocks a `@mock.patch` under it fills in -- both are
                    # leading positional parameters that are handed a value rather than injected.
                    positional_supplied=candidate.supplied_positionals + patching.positional_args,
                )
                cases = cases_for(marks.parametrizations) if marks.parametrizations else None
                # Folded here, inside this try, rather than in the loop below: a case's
                # `skipif`/`xfail` condition is a callable this calls, and one that raises is
                # this test's `CollectionError` exactly as a malformed graph is. One answer per
                # case rather than per record -- the fixture-case axis crossing it changes
                # neither the marks nor the reason.
                base_marks = decided(marks)
                per_case = [
                    (case, *_case_disposition(base_marks, case))
                    for case in (cases if cases is not None else (None,))
                ]
                expansions = expand_cases(plan)
            except Exception:
                # `plan_for` raises `DIError` for a malformed graph and, via `plan_of`, for an
                # unusable `Depends(...)` — declared twice, twice in one `Annotated[...]`, or
                # naming a fixture a stringified annotation cannot reach;
                # `known_params_of` raises `ValueError` for a name two stacked `@parametrize`s
                # both claim, and a case's own condition raises whatever it raises. All are
                # attributed to this test and collection continues, same as the `_skip_reason`
                # catch above.
                errors.append(CollectionError(path=relpath, message=traceback.format_exc()))
                continue

            # `cases` is `None` for a test with no `@velox.parametrize` mark; `expansions` always
            # has at least one entry, `case_id=None` when the test depends on no parametrized
            # fixture. The two axes are independent and cross freely: a test with neither gets one
            # un-suffixed record, exactly as before either axis existed.
            entries = [
                (
                    "-".join(
                        part for part in (expansion.case_id, case.id if case else None) if part
                    ),
                    case.params if case else None,
                    expansion.plan,
                    case_marks,
                    case_reason,
                )
                for expansion in expansions
                for case, case_marks, case_reason in per_case
            ]
            for case_id, params, record_plan, record_marks, case_reason in entries:
                name = f"{candidate.name}[{case_id}]" if case_id else candidate.name
                record_id = f"{relpath}::{name}"
                # Both filters run here rather than per function, above: a `-k` term and a
                # `path.py::test_name[case]` argument alike can name one case of a parametrized
                # test, which doesn't exist as an id until this expansion.
                if keyword_expr is not None and not keyword_expr.matches(record_id):
                    deselected.append(record_id)
                    continue
                if id_selection is not None and not id_selection.selects(resolved_path, name):
                    deselected.append(record_id)
                    continue
                # A case's own `skip`/`skipif`: the function's was answered before the expansion
                # and excluded the whole test there, so anything left here is one case's alone.
                if case_reason is not None:
                    skipped.append(Skipped(id=record_id, reason=case_reason, path=relpath))
                    continue
                if (
                    tag_expr is not None
                    and per_case_tags
                    and not tag_expr.matches(record_marks.tags)
                ):
                    deselected.append(record_id)
                    continue
                records.append(
                    TestRecord(
                        id=record_id,
                        index=index,
                        path=relpath,
                        lineno=defined.__code__.co_firstlineno,
                        qualname=func.__qualname__,
                        func=func,
                        params=params,
                        marks=record_marks,
                        plan=record_plan,
                        patches=patching.targets,
                    )
                )
                index += 1

    declaring_files.update(package_declarations)
    errors.extend(_misplaced_declarations(declaring_files, resolved_rootdir))
    return CollectionResult(
        records=records,
        errors=errors,
        skipped=skipped,
        deselected=deselected,
        unexpanded=unexpanded,
    )


def _package_declarations(
    path: Path,
    *,
    rootdir: Path,
    cache: dict[Path, tuple[Fixture[Any], ...] | None],
    errors: list[CollectionError],
) -> tuple[Fixture[Any], ...] | None:
    """What every package containing `path` declared with `velox.use(...)`, outermost first.

    `None` when one of those `__init__.py` files failed to import, in which case a
    `CollectionError` naming it has been appended to `errors` — once, however many test files sit
    under it. `cache` holds one entry per `__init__.py` and is what keeps a package imported
    exactly once across a whole `collect` call.
    """
    declared: list[Fixture[Any]] = []
    for init in package_inits(path, rootdir):
        if init not in cache:
            try:
                cache[init] = requires_of(_import_module(init, module_name_for(init, rootdir)))
            except Exception as error:
                cache[init] = None
                errors.append(
                    CollectionError(
                        path=display_path(init, Path(rootdir).resolve()),
                        message=_import_failure(error, init),
                    )
                )
        package = cache[init]
        if package is None:
            return None
        declared.extend(package)
    return tuple(declared)


def _misplaced_declarations(
    declaring_files: set[Path], resolved_rootdir: Path
) -> list[CollectionError]:
    """`velox.use(...)` declarations on modules that are neither collected test files nor
    packages above one.

    A declaration is read back off the modules velox imported for itself, so a call in a shared
    helper module is never seen and the fixtures it names never run. Reported rather than left as
    a missing side effect. A module velox imported reaches this scan only when something else
    imported it under its real name too, hence the `declaring_files` exemption; velox's own
    imports drop theirs again.

    The path is put through `display_path` like every other error's: one error type naming its
    file absolutely, where the rest name theirs relative to `rootdir`, is a path the reporter
    groups on its own and the run cache can never match against discovery.
    """
    misplaced: list[CollectionError] = []
    for name, module in list(sys.modules.items()):
        namespace = getattr(module, "__dict__", None)
        if namespace is None or not namespace.get(REQUIRES_ATTR):
            continue
        file = getattr(module, "__file__", None)
        if file is not None and Path(file).resolve() in declaring_files:
            continue
        misplaced.append(
            CollectionError(
                path=display_path(Path(file), resolved_rootdir) if file else Path(name),
                message=(
                    f"{name}: velox.use(...) applies to the tests in the container that calls it, "
                    f"and this module is neither a test module velox collects nor a package "
                    f"__init__.py above one. Move the call into a container that reaches the "
                    f"tests needing those fixtures."
                ),
            )
        )
    return misplaced


def _import_failure(error: Exception, path: Path) -> str:
    """The traceback for a failed import, plus what to do about the one failure mode velox's own
    import machinery causes.

    A file is imported by path under a synthetic `velox_tests.*` name with no real package behind
    it, so a relative import inside one resolves against nothing and fails naming `velox_tests` --
    a name the suite never wrote and can do nothing with.
    """
    message = traceback.format_exc()
    if isinstance(error, ModuleNotFoundError) and (error.name or "").startswith(_MODULE_PREFIX):
        message += (
            f"\nvelox imports {path} by its path rather than through its package, so a relative "
            f"import in it has nothing to resolve against. Import what it needs by absolute "
            f"module path instead.\n"
        )
    return message


def _no_dependencies() -> None:
    """Stand-in test body: a function whose plan is whatever `velox.use(...)` declared and
    nothing else."""


def _import_module(path: Path, module_name: str) -> object:
    """Import `path` under `module_name`, giving the installed rewrite hook first refusal.

    `importlib.util.spec_from_file_location` alone never consults `sys.meta_path` — only
    `import_module`/`__import__` run meta-path finders — so it would silently skip the assertion
    rewriter entirely. Asking the installed hook directly first, via `find_spec(module_name,
    [str(path.parent)])`, reproduces the directory-search contract `PathFinder` uses for a
    submodule import without needing `sys.path` or a real `velox_tests` package to exist. When the
    hook applies it hands back a spec with itself as the loader, and `exec_module` below runs the
    AST rewrite; when it doesn't (or no hook is installed), `find_spec` returns `None` and this
    falls back to plain `spec_from_file_location`. See `plans/rationale/collection.md` ("rewrite
    hook is consulted by hand") for the symlink edge case this leaves unresolved.

    importlib-only, no `sys.path` insertion. Any exception during `exec_module` propagates to the
    caller, which turns it into a `CollectionError`; the half-initialized module is removed from
    `sys.modules` first so a later, unrelated import of the same dotted name can't observe it.
    """
    hook = _rewrite.installed_hook()
    spec = hook.find_spec(module_name, [str(path.parent)]) if hook is not None else None
    if spec is None:
        spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build an import spec for {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise

    # Popped rather than left registered: `TestRecord.func` already holds the function object
    # directly (and, through `func.__globals__`, the module's own `__dict__`), so nothing
    # downstream looks this module up by name again, and leaving it would grow `sys.modules`
    # without bound across repeated in-process runs. A test that relies on its own module still
    # being `sys.modules`-resident after import will not find it there.
    sys.modules.pop(module_name, None)
    return module


def _definition_line(func: Callable[..., object]) -> int:
    """The line `func` is written on: the function underneath its decorators, so decorated and
    undecorated tests sort together in definition order."""
    return real_function(func).__code__.co_firstlineno


def _is_own_test_function(obj: object, module_name: str) -> bool:
    """Whether `obj` is a collectible test: a `def` or `async def` `test_*` function defined in
    this module.

    `inspect.isfunction` covers both -- an `async def` is a plain `FunctionType` with a flag on
    its code object, not a distinct type -- and, unlike `callable()`, excludes a class."""
    return (
        inspect.isfunction(obj)
        and getattr(obj, "__name__", "").startswith("test_")
        and getattr(obj, "__module__", None) == module_name
    )


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One test found in a module, before its marks, DI plan and parametrize cases are read."""

    name: str
    """The test id's tail: `test_name`, or `TestGroup::test_name` for a class-grouped test."""
    func: Callable[..., object]
    """What velox calls -- for a method, the `_receiving` wrapper that constructs its receiver."""
    supplied_positionals: int
    """Leading positional parameters `func` already fills in: a method's `self`/`cls`, and
    nothing else. `plan_for` adds whatever `mock.patch` supplies on top."""


def _module_candidates(module: object, module_name: str) -> tuple[list[_Candidate], list[str]]:
    """Every test one imported module defines, in source order, plus one message per shape that
    would otherwise contribute nothing at all.

    Module-level `test_*` functions and the `test_*` methods of every `class Test*` are gathered
    together and sorted by definition line, so a file mixing both reads in source order. Both are
    keyed on being defined in this module (`__module__`), so an imported helper is neither
    collected here nor mistaken for a broken test — with one exception, `_test_methods`' walk of
    a group's base classes, which is how a shared base contributes its tests to each group that
    inherits them.
    """
    candidates: list[_Candidate] = []
    problems: list[str] = []
    seen: set[int] = set()
    inherited_by_a_group = _group_bases(vars(module).values(), module_name)
    for name, obj in vars(module).items():
        if id(obj) in seen:
            # The same object bound twice (`test_alias = test_real`) is one test, collected
            # under the name it was defined with rather than once per binding.
            continue
        if _is_own_test_function(obj, module_name):
            seen.add(id(obj))
            candidates.append(_Candidate(name=obj.__name__, func=obj, supplied_positionals=0))
        elif inspect.isclass(obj) and getattr(obj, "__module__", None) == module_name:
            seen.add(id(obj))
            found, class_problems = _class_candidates(
                obj, prefix="", inherited_by_a_group=inherited_by_a_group
            )
            candidates.extend(found)
            problems.extend(class_problems)
        else:
            problem = _shape_problem(name, obj, module_name)
            if problem is not None:
                problems.append(problem)

    candidates.sort(key=lambda candidate: _definition_line(candidate.func))
    collectible: list[_Candidate] = []
    for candidate in candidates:
        if _yields(real_function(candidate.func)):
            # A generator function's body doesn't run when it is called -- velox would get an
            # iterator back, never iterate it, and report a test that passed without executing
            # a single line of itself.
            problems.append(
                f"{candidate.name}: a test that yields never runs its own body -- velox calls it "
                f"and gets a generator back. Write it as a plain test, and move anything it "
                f"yielded around into a fixture the test depends on."
            )
        else:
            collectible.append(candidate)
    return collectible, problems


def _group_bases(objects: Iterable[object], module_name: str) -> frozenset[type]:
    """Every class a `class Test*` in this module inherits from, however deep.

    A shared base holding `test_*` methods (`class SharedTests:`, inherited by
    `class TestPostgres(SharedTests)`) is not a group velox lost -- its tests run through each
    group that inherits them -- so `_misnamed_group_problem` leaves the base alone.
    """
    bases: set[type] = set()
    pending = [obj for obj in objects if inspect.isclass(obj) and obj.__name__.startswith("Test")]
    while pending:
        cls = pending.pop()
        bases.update(cls.__mro__[1:])
        pending.extend(
            member
            for member in vars(cls).values()
            if inspect.isclass(member)
            and member.__name__.startswith("Test")
            and getattr(member, "__module__", None) == module_name
        )
    return frozenset(bases)


def _class_candidates(
    cls: type, *, prefix: str, inherited_by_a_group: frozenset[type]
) -> tuple[list[_Candidate], list[str]]:
    """The tests one class contributes, `prefix` being the `::`-joined groups above it (empty at
    module level), plus a message per shape velox refuses to collect silently.

    A `class Test*` is pure namespacing. Its methods are collected with a receiver built per
    test, and nothing else about the class is honored — so a class whose shape asks for more than
    that (`__init__`, `setup_method`-style hooks, a mark, a `unittest.TestCase` base) contributes
    no tests at all and a `CollectionError` saying what to do instead, rather than tests running
    with half of what they were written to expect. A class defining no tests at all, directly or
    in a nested group, is left alone entirely: a helper named `TestServer` is ordinary code.
    """
    methods = _test_methods(cls)
    if not cls.__name__.startswith("Test"):
        problem = None if cls in inherited_by_a_group else _misnamed_group_problem(cls, methods)
        return [], [problem] if problem is not None else []

    group = f"{prefix}{cls.__name__}"
    nested_candidates: list[_Candidate] = []
    nested_problems: list[str] = []
    for member in vars(cls).values():
        if inspect.isclass(member) and getattr(member, "__module__", None) == cls.__module__:
            found, found_problems = _class_candidates(
                member, prefix=f"{group}::", inherited_by_a_group=inherited_by_a_group
            )
            nested_candidates.extend(found)
            nested_problems.extend(found_problems)

    if not methods and not nested_candidates and not nested_problems:
        return [], []

    problems = _class_problems(cls, methods)
    if problems:
        # Nested groups go with it: a class velox can't honor can't host them either.
        return [], problems

    candidates = [
        _Candidate(name=f"{group}::{name}", func=func, supplied_positionals=supplied)
        for name, func, supplied in methods
    ]
    return candidates + nested_candidates, nested_problems


#: Per-test and per-class lifecycle hooks velox has no equivalent of: a fixture is what runs
#: around a test. Named here so a migrated class carrying one is told, rather than running its
#: tests with the setup silently skipped. `setUp`/`tearDown` are `unittest`'s spelling, which
#: reaches this list only for a class that isn't a `TestCase` (that base is reported on its own).
_LIFECYCLE_HOOKS = (
    "setup_method",
    "teardown_method",
    "setup_class",
    "teardown_class",
    "setup",
    "teardown",
    "setUp",
    "tearDown",
)

#: Class-name endings that mean "this is a suite of tests" to a reader, and so to velox when it
#: has to decide whether a `test_*` method on a class it doesn't collect is a lost test or an
#: ordinary helper method that happens to be named that way.
_SUITE_NAME_ENDINGS = ("Test", "Tests", "TestCase", "TestCases", "TestSuite")


def _class_problems(cls: type, methods: list[tuple[str, Callable[..., object], int]]) -> list[str]:
    """What stops `cls`'s `test_*` methods from being collected, or an empty list.

    Read across the whole MRO, not just `cls` itself: an `__init__` or a `setup_method` on a base
    governs the group exactly as much as one written on it directly, and is just as invisible to
    the tests underneath.
    """
    problems: list[str] = []
    names = ", ".join(name for name, _, _ in methods) or "its tests"
    own = _class_namespace(cls)
    if _is_unittest_case(cls):
        # On its own, not alongside the checks below: `TestCase` brings an `__init__` and
        # `setUp`/`tearDown` with it, and repeating those as separate problems says nothing
        # the one message about the base doesn't already cover.
        return [_unittest_message(cls)]
    if "__init__" in own:
        problems.append(
            f"{cls.__qualname__}: velox constructs a test class with no arguments, once per test, "
            f"and this one defines __init__. Move what it sets up into a fixture {names} depend "
            f"on, or rename the class so velox doesn't collect it."
        )
    hooks = [hook for hook in _LIFECYCLE_HOOKS if hook in own]
    if hooks:
        problems.append(
            f"{cls.__qualname__}: {', '.join(hooks)} would never run -- a test class is pure "
            f"namespacing, with no lifecycle of its own. Move that setup into a fixture {names} "
            f"depend on, with teardown after its `yield`."
        )
    if marks_of(cls) != Marks():
        problems.append(
            f"{cls.__qualname__}: a mark on a class doesn't reach the tests inside it. Apply "
            f"@velox.skip/@velox.tag/... to each of {names} instead."
        )
    return problems


def _misnamed_group_problem(
    cls: type, methods: list[tuple[str, Callable[..., object], int]]
) -> str | None:
    """The message for a class that reads as a suite of tests but isn't named `Test*`, or `None`.

    A class that merely happens to have a `test_*` method (a fake client with a
    `test_connection`, say) is ordinary code, so only a `unittest.TestCase` or a name ending in
    `Test`/`Tests`/`TestCase` is reported -- narrow on purpose, since the alternative to silence
    here is a false collection error on working code.
    """
    if not methods:
        return None
    if _is_unittest_case(cls):
        return _unittest_message(cls)
    if not cls.__name__.endswith(_SUITE_NAME_ENDINGS):
        return None
    names = ", ".join(name for name, _, _ in methods)
    return (
        f"{cls.__qualname__}: velox collects test methods from classes named `Test*` only, so "
        f"{names} would never run. Rename it to `Test{cls.__name__}`, or move its tests to "
        f"module-level functions."
    )


def _unittest_message(cls: type) -> str:
    return (
        f"{cls.__qualname__}: unittest-style test classes aren't collected. Rewrite its tests as "
        f"functions, or as methods on a plain `class Test*` that doesn't subclass "
        f"unittest.TestCase, with any setUp work moved into a fixture."
    )


def _is_unittest_case(cls: type) -> bool:
    """Whether `cls` subclasses `unittest.TestCase`.

    Read off `sys.modules` rather than imported: a suite that never touched `unittest` can have
    no `TestCase` subclass in it, and this way collection doesn't import one to find that out.
    """
    unittest = sys.modules.get("unittest")
    case = getattr(unittest, "TestCase", None) if unittest is not None else None
    return case is not None and issubclass(cls, case)


def _class_namespace(cls: type) -> dict[str, Any]:
    """`cls`'s own attributes plus everything it inherits (`object`'s excluded), with a derived
    class's definition winning over the base it overrides.

    Reversed MRO, not `dir()`: this keeps the raw class-body object for each name -- the
    `staticmethod`/`classmethod` descriptor rather than what attribute access turns it into --
    which is what `_test_methods` reads to tell one kind of method from another.
    """
    namespace: dict[str, Any] = {}
    for klass in reversed(cls.__mro__[:-1]):
        namespace.update(vars(klass))
    return namespace


def _test_methods(cls: type) -> list[tuple[str, Callable[..., object], int]]:
    """The `test_*` methods `cls` provides, inherited ones included: each one's name, what velox
    calls, and how many leading positional parameters that call already fills in.

    A shared base of test methods is an ordinary class, so its tests belong to every group that
    inherits them -- and a group that inherits and overrides one collects the override, since
    `_class_namespace` resolves the MRO the way an attribute lookup would.

    `@staticmethod`/`@classmethod` wrap the function in a descriptor, so a class body doesn't hand
    back a plain `FunctionType` for those the way it does for an ordinary method -- unwrapped via
    `__func__` first, so a `test_*` method under either decorator is collected rather than
    silently missed. A static method is called exactly as written; every other form goes through
    `_receiving`, which supplies a fresh instance (or, for a class method, the class itself).
    """
    methods: list[tuple[str, Callable[..., object], int]] = []
    for name, member in _class_namespace(cls).items():
        if not name.startswith("test_"):
            continue
        if isinstance(member, staticmethod):
            if inspect.isfunction(member.__func__):
                methods.append((name, member.__func__, 0))
        elif isinstance(member, classmethod):
            if inspect.isfunction(member.__func__):
                methods.append((name, _receiving(member.__func__, lambda: cls), 1))
        elif inspect.isfunction(member):
            methods.append((name, _receiving(member, cls), 1))
    return methods


def _receiving(func: Callable[..., Any], receiver: Callable[[], object]) -> Callable[..., object]:
    """`func` as velox calls it, with `receiver()` passed as its first argument per call.

    A test class is namespacing, so the instance a method runs on is built for that one test and
    thrown away — nothing reaches another test through `self`, which is what keeps class-grouped
    tests as independent as module-level ones under concurrent dispatch.

    The wrapper carries `func`'s name, qualname and marks and points `__wrapped__` at it, which
    is what lets `real_function`, `patching_of` and `marks_of` see through it exactly as they see
    through an ordinary decorator. `functools.wraps` is deliberately not used: it copies
    `__dict__` wholesale, and `mock.patch`'s own `patchings` list living there would then be seen
    twice — once on the wrapper and once on the patched function under it — doubling both the
    reported patch targets and the positional arguments they are counted as supplying.
    """
    if inspect.iscoroutinefunction(func):

        async def call(**kwargs: Any) -> object:
            return await func(receiver(), **kwargs)
    else:

        def call(**kwargs: Any) -> object:
            return func(receiver(), **kwargs)

    call.__name__ = func.__name__
    call.__qualname__ = func.__qualname__
    call.__module__ = func.__module__
    call.__doc__ = func.__doc__
    call.__wrapped__ = func  # type: ignore[attr-defined]
    marks = getattr(func, "__dict__", {}).get(MARKS_ATTR)
    if marks is not None:
        setattr(call, MARKS_ATTR, marks)
    return call


def _shape_problem(name: str, obj: object, module_name: str) -> str | None:
    """The message for a module-level `test_*` name bound to a function velox doesn't collect, or
    `None`.

    The shape reported is a function body this module wrote as a test but bound under a name that
    isn't its own -- `test_x = lambda: ...`, `test_x = _impl` -- which contributes nothing and
    says nothing. Only functions are reported: `test_app = FastAPI()` and `test_client = Mock()`
    are objects a test uses, and they are callable, so anything wider than this turns ordinary
    code into a collection error. A `test_*` function imported from another module isn't reported
    either -- it belongs to the module that defines it, and not collecting it here is what keeps
    it from being collected twice.
    """
    if not name.startswith("test_") or not inspect.isfunction(obj):
        return None
    if getattr(obj, "__module__", None) != module_name or obj.__name__.startswith("test_"):
        return None
    return (
        f"{name}: velox collects `def`/`async def` functions whose own name starts with `test_`, "
        f"and this name is bound to the function {obj.__name__!r}, so it would run as no test at "
        f"all. Define it with `def {name}(...)`."
    )


def _yields(func: Callable[..., object]) -> bool:
    """Whether calling `func` returns a generator instead of running its body."""
    return inspect.isgeneratorfunction(func) or inspect.isasyncgenfunction(func)
