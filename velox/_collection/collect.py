"""Collection: import test modules and build the flat list of test records.

`TestRecord` carries what the runner needs to execute a test: `id`, `path`, `lineno`, `qualname`,
`func`, its `params` (if `@velox.parametrize`d), and its resolved `plan`. A test marked
`@velox.skip`/`@velox.skipif` is read off the function object and excluded from `records` into
`skipped` instead of running for real; a `tag_expr` (see `tagexpr.py`) whose expression a test's
`@velox.tag(...)` names don't satisfy excludes it into `deselected` instead -- checked after the
skip check, so a skip-marked test is always `skipped`, never reclassified as deselected depending
on `-m`. A `Depends(...)`-defaulted parameter is resolved via `_fixtures.plan_for`, alongside the
fixtures the module declared for all of its tests with `velox.use(...)` (`requires.py`), and a
malformed DI graph (bad scope nesting, a missing injection) becomes a `CollectionError`, the same
way a bad import does. A `@velox.parametrize`d test expands into one record per case
(`parametrize.cases_for`), sharing the one `ResolutionPlan` built for the function — parametrize
values are call kwargs, not part of the DI graph. A test transitively depending on a fixture built
with `params=` expands the same way, on the DI graph instead (`_fixtures.expand_cases`); the two
axes cross freely, so both together multiply.

A decorated test is read through its decorators: its injection plan and its definition line come
from the function underneath (`_mocking.real_function`), while the decorated object is what runs.
`unittest.mock` patching found on the way down (`_mocking.patching_of`) lands on `TestRecord` as
`patches`, which is what schedules the test alone.

Import mechanics: importlib only, path-derived module names under `velox_tests.*`, one entry per
module never touching `sys.path`, an exception during `exec_module` becomes a `CollectionError`
attributed to that file rather than aborting the run. The assertion-rewriting meta-path hook, when
installed, is consulted explicitly (`_import_module`) — `spec_from_file_location` alone never
gives it the chance to run.

Both `async def test_*` and plain `def test_*` functions are collected; `_run.py` runs the sync
ones on the context-propagating executor rather than inline. A `class Test*` carrying a `test_*`
method becomes a `CollectionError` naming the class, the same way a bad import does.
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

from velox._assertions import rewrite as _rewrite
from velox._collection.parametrize import cases_for, known_params_of
from velox._collection.requires import REQUIRES_ATTR, requires_of
from velox._collection.tagexpr import TagExpression
from velox._di.fixtures import ResolutionPlan, expand_cases, plan_for
from velox._marks import Marks, marks_of
from velox._mocking import patching_of, real_function

__all__ = [
    "CollectionError",
    "CollectionResult",
    "Skipped",
    "TestRecord",
    "collect",
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
    patches: tuple[str, ...] = ()
    """What this test patches with `unittest.mock`, one display name per patcher
    (`_mocking.patching_of`). Non-empty means the test installs a process-global override and
    is scheduled to run alone."""


@dataclass(frozen=True, slots=True)
class CollectionError:
    """An import failure attributed to one file, a test whose dependency graph is malformed
    (bad scope nesting, a missing injection), or a `class Test*` carrying `test_*` methods."""

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


@dataclass(frozen=True, slots=True)
class CollectionResult:
    records: list[TestRecord]
    errors: list[CollectionError]
    skipped: list[Skipped]
    deselected: list[str] = field(default_factory=list)
    """Ids of tests excluded by `tag_expr`, not `skipped`: these passed the skip check (they
    would otherwise run) but never reached the DI checks."""


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


def _display_path(path: Path, resolved_rootdir: Path) -> Path:
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
        condition = skipif.condition
        if condition() if callable(condition) else condition:
            return skipif.reason
    return None


def collect(
    files: Iterable[Path], *, rootdir: Path, tag_expr: TagExpression | None = None
) -> CollectionResult:
    """Import each file and build its records; `index` assigned once over the whole result.

    Per file, in the order given:

    1. Compute the module name (`module_name_for`) and import it (`_import_module`, which
       consults the installed assertion-rewriting hook first). An exception here becomes a
       `CollectionError`; the file contributes zero records and collection continues to the next
       file.
    2. Within the imported module, find `test_*` functions *defined* in it — i.e.
       `getattr(obj, "__module__", None) == module.__name__`, so a `test_*` helper imported from
       elsewhere isn't collected twice, nor given the importing module's `velox.use(...)`
       declarations. A `class Test*` defined in the module and carrying its own `test_*` method
       becomes a `CollectionError` naming the class and its methods, one per class.
    3. Sort the functions by definition line (`_definition_line`, read through any decorators) —
       definition order, not `vars()` iteration order.
    4. Per function: a `skip`/truthy-`skipif` mark excludes it from `records` into `skipped`
       instead. Otherwise `tag_expr`, if given, excludes a test whose `@velox.tag(...)` names
       don't satisfy it into `deselected`. Otherwise a malformed DI graph — its own, or one the
       module's `velox.use(...)` fixtures introduce — or a name collision between stacked
       `@parametrize`s, excludes it into `errors` instead. Otherwise build one
       `TestRecord` per case in the cartesian product of `@velox.parametrize`'s cases and the
       fixture graph's own parametrized-fixture cases (one of each, for a function using
       neither), `id` as `"{path}::{qualname}"`, or `"{path}::{qualname}[{case_id}]"` when either
       axis contributes, with `path` relative to `rootdir` and each record carrying the
       `ResolutionPlan` specialized for its own fixture-case combination.

    A module's `velox.use(...)` declarations are validated once, before its tests: a malformed
    declared graph is one `CollectionError` for the file, which then contributes no records.
    Once every file is done, a `velox.use(...)` call left on a module that isn't a collected test
    file becomes a `CollectionError` of its own (`_misplaced_declarations`).

    `files` is assumed already de-duplicated and in deterministic order (`discover_files` gives
    you both); `index` is assigned across the concatenation of all files' records, in that order
    -- deselected tests never consume an index.
    """
    records: list[TestRecord] = []
    errors: list[CollectionError] = []
    skipped: list[Skipped] = []
    deselected: list[str] = []
    index = 0
    resolved_rootdir = Path(rootdir).resolve()

    collected_files: set[Path] = set()

    for path in files:
        collected_files.add(Path(path).resolve())
        display_path = _display_path(path, resolved_rootdir)
        module_name = module_name_for(path, rootdir)
        try:
            module = _import_module(path, module_name)
        except Exception:
            # Attributed to the file, not raised: one broken test module must not take the
            # rest of the suite down with it.
            errors.append(CollectionError(path=display_path, message=traceback.format_exc()))
            continue

        implicit = requires_of(module)
        if implicit:
            try:
                # Validated once against a stand-in body rather than per test: the declared graph
                # is the same for all of them, so the alternative is one identical traceback per
                # test in the file.
                plan_for(_no_dependencies, implicit=implicit)
            except Exception:
                errors.append(CollectionError(path=display_path, message=traceback.format_exc()))
                continue

        functions = [
            obj for obj in vars(module).values() if _is_own_test_function(obj, module_name)
        ]
        functions.sort(key=_definition_line)

        classes = [obj for obj in vars(module).values() if _is_own_test_class(obj, module_name)]
        classes.sort(key=lambda cls: cls.__qualname__)
        for cls in classes:
            methods = ", ".join(sorted(_test_method_names(cls)))
            errors.append(
                CollectionError(
                    path=display_path,
                    message=(
                        f"{cls.__qualname__}: class-based test collection isn't supported; move "
                        f"{methods} to module-level functions (see ROADMAP.md)."
                    ),
                )
            )

        for func in functions:
            test_id = f"{display_path}::{func.__qualname__}"
            marks = marks_of(func)
            try:
                reason = _skip_reason(marks)
            except Exception:
                # One test's malformed marks must not abort the file's remaining tests any more
                # than a broken import aborts the remaining files.
                errors.append(CollectionError(path=display_path, message=traceback.format_exc()))
                continue

            if reason is not None:
                # Ahead of tag_expr: a test marked skip is skipped for the reason it gives,
                # regardless of -m -- @velox.skip is never silently reclassified as deselected
                # depending on which tags happen to be in play.
                skipped.append(Skipped(id=test_id, reason=reason))
                continue

            if tag_expr is not None and not tag_expr.matches(marks.tags):
                deselected.append(test_id)
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
                    positional_supplied=patching.positional_args,
                )
                cases = cases_for(marks.parametrizations) if marks.parametrizations else None
                expansions = expand_cases(plan)
            except Exception:
                # `plan_for` raises `DIError` for a malformed graph and, via `plan_of`, a plain
                # `TypeError` for a stray `Depends(...)` inside `Annotated[...]` metadata;
                # `known_params_of` raises `ValueError` for a name two stacked `@parametrize`s
                # both claim. All are attributed to this test and collection continues, same as
                # the `_skip_reason` catch above.
                errors.append(CollectionError(path=display_path, message=traceback.format_exc()))
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
                )
                for expansion in expansions
                for case in (cases if cases is not None else (None,))
            ]
            for case_id, params, record_plan in entries:
                records.append(
                    TestRecord(
                        id=f"{test_id}[{case_id}]" if case_id else test_id,
                        index=index,
                        path=display_path,
                        lineno=defined.__code__.co_firstlineno,
                        qualname=func.__qualname__,
                        func=func,
                        params=params,
                        plan=record_plan,
                        patches=patching.targets,
                    )
                )
                index += 1

    errors.extend(_misplaced_declarations(collected_files))
    return CollectionResult(records=records, errors=errors, skipped=skipped, deselected=deselected)


def _misplaced_declarations(collected_files: set[Path]) -> list[CollectionError]:
    """`velox.use(...)` declarations on modules that aren't test files velox collected.

    A declaration is read back off the test module velox imported it from, so a call in a shared
    helper module is never seen and the fixtures it names never run. Reported rather than left as
    a missing side effect. A collected test module reaches `sys.modules` only when something else
    imported it under its real name too, hence the `collected_files` exemption; velox's own import
    drops it again (`_import_module`).
    """
    misplaced: list[CollectionError] = []
    for name, module in list(sys.modules.items()):
        namespace = getattr(module, "__dict__", None)
        if namespace is None or not namespace.get(REQUIRES_ATTR):
            continue
        file = getattr(module, "__file__", None)
        if file is not None and Path(file).resolve() in collected_files:
            continue
        misplaced.append(
            CollectionError(
                path=Path(file) if file else Path(name),
                message=(
                    f"{name}: velox.use(...) applies to the tests in the module that calls it, "
                    f"and this module isn't one velox collects. Move the call into each test "
                    f"module that needs those fixtures."
                ),
            )
        )
    return misplaced


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
    falls back to plain `spec_from_file_location`. See `docs/rationale.md` ("meta-path hook
    consulted directly") for the symlink edge case this leaves unresolved.

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


def _is_own_test_class(obj: object, module_name: str) -> bool:
    """Whether `obj` is a `class Test*` defined in this module and carrying its own `test_*`
    method — the shape `collect` reports as a `CollectionError` rather than collecting nothing."""
    return (
        inspect.isclass(obj)
        and getattr(obj, "__name__", "").startswith("Test")
        and getattr(obj, "__module__", None) == module_name
        and bool(_test_method_names(obj))
    )


def _test_method_names(cls: type) -> list[str]:
    """The `test_*`-named methods defined directly on `cls`, unsorted.

    `@staticmethod`/`@classmethod` wrap the function in a descriptor, so `vars(cls)` doesn't hand
    back a plain `FunctionType` for those the way it does for an ordinary method -- unwrapped via
    `__func__` before the `isfunction` check, so a `test_*` method under either decorator is still
    caught rather than silently missed.
    """
    names = []
    for name, member in vars(cls).items():
        if isinstance(member, (staticmethod, classmethod)):
            member = member.__func__
        if inspect.isfunction(member) and name.startswith("test_"):
            names.append(name)
    return names
