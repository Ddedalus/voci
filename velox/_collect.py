"""Collection: import test modules and build the flat list of test records (spec/03 §3-4).

`TestRecord` here is still smaller than the full shape in spec/03 §1 — it carries what the M0/M1
runner needs (`id`, `path`, `lineno`, `qualname`, `func`, and now `plan`). The rest of `MarkSet`/
`exclusive` land alongside the scheduler that reads them; growing this dataclass further towards
the spec shape is future work, not a thing to guess at now. Two exceptions, because leaving them
unhandled is a *wrong answer* rather than a missing feature (I8): `@velox.skip`/`skipif` are
already public API, so a marked test is read off the function object and excluded from `records`
instead of silently running for real (see `Skipped`); and, as of M1, a `Depends(...)`-defaulted
parameter is resolved via `_fixtures.plan_for` rather than refused outright — a *malformed* DI
graph (bad scope nesting, a missing injection) still becomes a `CollectionError`, exactly the
way a bad import does, but a well-formed one now produces a real `ResolutionPlan` on the record
instead of being turned away wholesale.

Import mechanics follow spec/03 §3: importlib only, one position, path-derived module names
under `velox_tests.*`, an exception during `exec_module` becomes a `CollectionError` attributed
to that file rather than aborting the run. The assertion-rewriting meta-path hook, when
installed, is consulted explicitly (`_import_module`) — `spec_from_file_location` alone never
gives it the chance spec/03 §3 step 2 describes.

Only `async def test_*` functions are collected (spec/01 §2 — "Only async def tests are supported
in MVP"). A sync `test_*` is silently left uncollected for now; a loud diagnostic for that case is
roadmap, not implemented yet (there is no reporter machinery to hang a warning off yet).
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import re
import sys
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from velox import _rewrite
from velox._fixtures import ResolutionPlan, plan_for
from velox._marks import Marks, marks_of

__all__ = [
    "CollectionError",
    "CollectionResult",
    "Skipped",
    "TestRecord",
    "collect",
    "module_name_for",
]

#: Everything under this prefix is a velox-imported test module (spec/03 §3 step 1). Never
#: `sys.path`-relative — the whole point is that two `test_utils.py` in different directories
#: get different dotted names instead of colliding.
_MODULE_PREFIX = "velox_tests"

#: What survives unescaped in a dotted module segment.
_NON_IDENTIFIER_CHARS = re.compile(r"[^0-9A-Za-z_]")


@dataclass(frozen=True, slots=True)
class TestRecord:
    """One collected test. See module docstring for why this is smaller than spec/03 §1."""

    id: str
    index: int
    path: Path
    lineno: int
    qualname: str
    func: Callable[..., object]
    plan: ResolutionPlan
    """This test function's whole transitive fixture graph, already resolved (spec/04 §1). Every
    record carries one, even a test with no `Depends(...)` at all (`steps=()`, `root_args=()`) —
    uniformity here is what keeps `_run.py` a single code path instead of an "if it has fixtures"
    branch."""


@dataclass(frozen=True, slots=True)
class CollectionError:
    """An import failure attributed to one file (spec/03 §3 step 3) — or, in M0's extension of
    that idea, one test this milestone knows it cannot run correctly (an unsupported
    `Depends(...)` parameter)."""

    path: Path
    message: str


@dataclass(frozen=True, slots=True)
class Skipped:
    """A collected test excluded from `records` because a skip mark said so — contrast
    `CollectionError`, which means something is *wrong*; this means the suite asked, correctly,
    for the test not to run.

    M0's `Outcome` enum has no `skipped` member (spec/05 §4's full enum is M1), so this can't be
    a `TestResult`. Reporting it as its own list is the smallest change that stops
    `@velox.skip`/`@velox.skipif` from being silently ignored and the test running for real.
    """

    id: str
    reason: str


@dataclass(frozen=True, slots=True)
class CollectionResult:
    records: list[TestRecord]
    errors: list[CollectionError]
    skipped: list[Skipped]


def module_name_for(path: Path, rootdir: Path) -> str:
    """`velox_tests.<dotted.relpath.without.suffix>` (spec/03 §3 step 1).

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

    spec/03 §1 specifies `TestRecord.path` "relative to rootdir", and ids built from an absolute
    path are machine-specific — a `--deselect`/`-k`/JUnit key and I2's byte-identical-output goal
    can't survive that. Falls back to the resolved absolute path for a file outside `rootdir` (an
    explicit argument elsewhere on disk), the same case `module_name_for` falls back on.
    """
    resolved = path.resolve()
    try:
        return resolved.relative_to(resolved_rootdir)
    except ValueError:
        return resolved


def _skip_reason(marks: Marks) -> str | None:
    """The reason this test should not run, or `None`. `skip` always wins; among `skipifs`
    (which legitimately stack), the first truthy condition's reason is used — spec/01 §4's "any
    one truthy condition skips", not "the last one wins"."""
    if marks.skip is not None:
        return marks.skip.reason
    for skipif in marks.skipifs:
        condition = skipif.condition
        if condition() if callable(condition) else condition:
            return skipif.reason
    return None


def collect(files: Iterable[Path], *, rootdir: Path) -> CollectionResult:
    """Import each file and build its records; `index` assigned once over the whole result.

    Per file, in the order given (spec/03 §3-4):

    1. Compute the module name (`module_name_for`) and import (`_import_module`, which consults
       the installed assertion-rewriting hook first). An exception here becomes a
       `CollectionError`; the file contributes zero records and collection continues to the next
       file (spec/03 §3 step 3).
    2. Within the imported module, find `async def test_*` functions *defined* in it — i.e.
       `getattr(obj, "__module__", None) == module.__name__`, so a `test_*` helper imported from
       elsewhere isn't collected twice (spec/03 §4 step 1).
    3. Sort those by `func.__code__.co_firstlineno` — definition order, not `vars()` iteration
       order (spec/03 §4 step 2).
    4. Per function: a `skip`/truthy-`skipif` mark excludes it from `records` into `skipped`
       instead; a malformed DI graph (`_fixtures.plan_for` raising `DIError` — bad scope nesting,
       a missing injection) excludes it into `errors` instead. Otherwise build one `TestRecord`,
       `id` as `"{path}::{qualname}"` with `path` relative to `rootdir`, carrying the
       `ResolutionPlan` `plan_for` built.

    `files` is assumed already de-duplicated and in deterministic order (`discover_files` gives
    you both); `index` is assigned across the concatenation of all files' records, in that order
    (spec/03 §4).
    """
    records: list[TestRecord] = []
    errors: list[CollectionError] = []
    skipped: list[Skipped] = []
    index = 0
    resolved_rootdir = Path(rootdir).resolve()

    for path in files:
        display_path = _display_path(path, resolved_rootdir)
        module_name = module_name_for(path, rootdir)
        try:
            module = _import_module(path, module_name)
        except Exception:
            # Attributed to the file, not raised: one broken test module must not take the
            # rest of the suite down with it (spec/03 §3 step 3).
            errors.append(CollectionError(path=display_path, message=traceback.format_exc()))
            continue

        functions = [
            obj for obj in vars(module).values() if _is_own_test_function(obj, module_name)
        ]
        functions.sort(key=lambda func: func.__code__.co_firstlineno)

        for func in functions:
            test_id = f"{display_path}::{func.__qualname__}"
            try:
                reason = _skip_reason(marks_of(func))
            except Exception:
                # One test's malformed marks must not abort the file's remaining tests any more
                # than a broken import aborts the remaining files.
                errors.append(CollectionError(path=display_path, message=traceback.format_exc()))
                continue

            if reason is not None:
                skipped.append(Skipped(id=test_id, reason=reason))
                continue

            try:
                plan = plan_for(func)
            except Exception:
                # `plan_for` raises `DIError` for a malformed graph (bad scope nesting, a
                # missing injection, spec/04 §2) and, via `plan_of`, a plain `TypeError` for a
                # stray `Depends(...)` inside `Annotated[...]` metadata (spec/01 rule 3). Both
                # are attributed to this test and collection continues — same reasoning as the
                # `marks_of` catch above, and the same broad `except Exception` so a new static
                # check added to `plan_for` later doesn't need a matching new `except` clause
                # here to stay loud instead of aborting the whole file.
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

    return CollectionResult(records=records, errors=errors, skipped=skipped)


def _import_module(path: Path, module_name: str) -> object:
    """Import `path` under `module_name`, giving the installed rewrite hook first refusal.

    `importlib.util.spec_from_file_location` alone — the M0-skeleton's original approach — never
    consults `sys.meta_path`: only `import_module`/`__import__` run meta-path finders, so
    `cli.main` installing `AssertionRewritingHook` had no effect on modules imported this way
    (verified: a failing rewritten-looking assert produced a bare `AssertionError`, no
    explanation, despite `assertions: rewrite` in the header). Fixed by asking the installed hook
    directly first: `MetaPathFinder.find_spec(name, path, target)` takes `path` as the list of
    *directories* to search for the name's last dotted component in — the same contract
    `PathFinder` uses for a submodule import — so `[str(path.parent)]` reproduces that without
    needing `sys.path` or a real `velox_tests` package to exist. When the hook applies (matches
    `fnpats` — `test_*.py`/`*_test.py`, which is exactly what `discover_files` already filtered
    by — or `conftest.py`, or `isinitpath`) it hands back a spec with itself as the loader, and
    `exec_module` below runs the AST rewrite. When it doesn't (declines, or no hook installed —
    `--assert=plain` or a failed cache probe), `find_spec` returns `None` and this falls back to
    the plain `spec_from_file_location` path exactly as before.

    Residual gap, not chased further here: `isinitpath` compares `os.path.abspath` (no symlink
    resolution, matching the vendored shim's `absolutepath`) against paths `discover_files`
    produced via `.resolve()` (which does resolve symlinks) — under a symlinked root the two
    could disagree and `isinitpath` would miss. It doesn't matter for any file `discover_files`
    finds on its own, since those already match `fnpats` independent of `isinitpath`; it would
    only matter for an *explicit* file argument whose name doesn't match the test-file patterns,
    reached through a symlinked path. Unifying the two path conventions touches `_rewrite.py`'s
    own walk and its agreement with the vendored `absolutepath()`, which is more surgery than
    this fix needs to take on.

    importlib-only, no `sys.path` insertion (spec/03 §3 step 2). Any exception during
    `exec_module` propagates to the caller, which turns it into a `CollectionError`; the
    half-initialized module is removed from `sys.modules` first so a later, unrelated import of
    the same dotted name can't observe it.
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

    # Nothing downstream looks this module up by name again — `TestRecord.func` holds the
    # function object directly, and through `func.__globals__` a direct reference to the
    # module's own `__dict__`, so popping it here doesn't break anything that runs later.
    # Leaving it registered would mean every `main()` call in a process permanently grows
    # `sys.modules` with another `velox_tests.*` entry (this repo's own suite calls `main()`
    # many times over in-process) — real, unbounded-with-run-count memory, and it pins every
    # module-level object the file created for the rest of the process. Trade-off, stated
    # rather than hidden: a test that relies on its *own* module still being `sys.modules`-
    # resident while it runs (pickling an instance defined in it, a dynamic re-import of
    # `__name__`) will not find it there. Nothing in this codebase does that today.
    sys.modules.pop(module_name, None)
    return module


def _is_own_test_function(obj: object, module_name: str) -> bool:
    """Whether `obj` is a collectible test: an `async def test_*` defined in this module."""
    return (
        inspect.iscoroutinefunction(obj)
        and getattr(obj, "__name__", "").startswith("test_")
        and getattr(obj, "__module__", None) == module_name
    )
