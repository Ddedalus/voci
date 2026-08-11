"""Collection: import test modules and build the flat list of test records.

`TestRecord` carries what the runner needs to execute a test: `id`, `path`, `lineno`, `qualname`,
`func`, and its resolved `plan`. A test marked `@velox.skip`/`@velox.skipif` is read off the
function object and excluded from `records` into `skipped` instead of running for real; a
`Depends(...)`-defaulted parameter is resolved via `_fixtures.plan_for`, and a malformed DI graph
(bad scope nesting, a missing injection) becomes a `CollectionError`, the same way a bad import
does. A `tag_expr` (see `tagexpr.py`) whose expression a test's `@velox.tag(...)` names don't
satisfy excludes it into `deselected` instead -- checked after the skip check, so a skip-marked
test is always `skipped`, never reclassified as deselected depending on `-m`.

Import mechanics: importlib only, path-derived module names under `velox_tests.*`, one entry per
module never touching `sys.path`, an exception during `exec_module` becomes a `CollectionError`
attributed to that file rather than aborting the run. The assertion-rewriting meta-path hook, when
installed, is consulted explicitly (`_import_module`) — `spec_from_file_location` alone never
gives it the chance to run.

Both `async def test_*` and plain `def test_*` functions are collected; `_run.py` runs the sync
ones on the context-propagating executor rather than inline. A `test_*` method on a `class Test*`
is silently left uncollected.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import re
import sys
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from velox._assertions import rewrite as _rewrite
from velox._collection.tagexpr import TagExpression
from velox._di.fixtures import ResolutionPlan, plan_for
from velox._marks import Marks, marks_of

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
    plan: ResolutionPlan
    """This test function's whole transitive fixture graph, already resolved. Every record
    carries one, even a test with no `Depends(...)` at all (`steps=()`, `root_args=()`) —
    uniformity here is what keeps the runner a single code path instead of an "if it has
    fixtures" branch."""


@dataclass(frozen=True, slots=True)
class CollectionError:
    """An import failure attributed to one file, or a test whose dependency graph is malformed
    (bad scope nesting, a missing injection)."""

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
       elsewhere isn't collected twice.
    3. Sort those by `func.__code__.co_firstlineno` — definition order, not `vars()` iteration
       order.
    4. Per function: a `skip`/truthy-`skipif` mark excludes it from `records` into `skipped`
       instead. Otherwise `tag_expr`, if given, excludes a test whose `@velox.tag(...)` names
       don't satisfy it into `deselected`. Otherwise a malformed DI graph excludes it into
       `errors` instead. Otherwise build one `TestRecord`, `id` as `"{path}::{qualname}"` with
       `path` relative to `rootdir`, carrying the `ResolutionPlan` `plan_for` built.

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

    for path in files:
        display_path = _display_path(path, resolved_rootdir)
        module_name = module_name_for(path, rootdir)
        try:
            module = _import_module(path, module_name)
        except Exception:
            # Attributed to the file, not raised: one broken test module must not take the
            # rest of the suite down with it.
            errors.append(CollectionError(path=display_path, message=traceback.format_exc()))
            continue

        functions = [
            obj for obj in vars(module).values() if _is_own_test_function(obj, module_name)
        ]
        functions.sort(key=lambda func: func.__code__.co_firstlineno)

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

            try:
                plan = plan_for(func)
            except Exception:
                # `plan_for` raises `DIError` for a malformed graph and, via `plan_of`, a plain
                # `TypeError` for a stray `Depends(...)` inside `Annotated[...]` metadata. Both
                # are attributed to this test and collection continues, same as the `_skip_reason`
                # catch above.
                errors.append(CollectionError(path=display_path, message=traceback.format_exc()))
                continue

            records.append(
                TestRecord(
                    id=test_id,
                    index=index,
                    path=display_path,
                    lineno=func.__code__.co_firstlineno,
                    qualname=func.__qualname__,
                    func=func,
                    plan=plan,
                )
            )
            index += 1

    return CollectionResult(records=records, errors=errors, skipped=skipped, deselected=deselected)


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


def _is_own_test_function(obj: object, module_name: str) -> bool:
    """Whether `obj` is a collectible test: a `def` or `async def` `test_*` function defined in
    this module.

    `inspect.isfunction` covers both -- an `async def` is a plain `FunctionType` with a flag on
    its code object, not a distinct type -- and, unlike `callable()`, excludes a `class Test*`
    (see `ROADMAP.md`)."""
    return (
        inspect.isfunction(obj)
        and getattr(obj, "__name__", "").startswith("test_")
        and getattr(obj, "__module__", None) == module_name
    )
