"""Collection: import test modules and build the flat list of test records (spec/03 §3-4).

M0 scope only. `TestRecord` here is deliberately smaller than the full shape in spec/03 §1 — it
carries just what M0's runner needs (`id`, `path`, `lineno`, `qualname`, `func`). `marks`, `plan`,
and `exclusive` land in M1 alongside the DI graph and the scheduler that read them; growing this
dataclass towards the spec shape is M1's job, not a thing to guess at now.

Import mechanics follow spec/03 §3 verbatim: importlib only, one position, path-derived module
names under `velox_tests.*`, an exception during `exec_module` becomes a `CollectionError`
attributed to that file rather than aborting the run.

Only `async def test_*` functions are collected (spec/01 §2 — "Only async def tests are supported
in MVP"). A sync `test_*` is silently left uncollected for now; a loud diagnostic for that case is
roadmap, not M0 (M0 has no reporter machinery to hang a warning off yet).
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["CollectionError", "CollectionResult", "TestRecord", "collect", "module_name_for"]

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


@dataclass(frozen=True, slots=True)
class CollectionError:
    """An import failure attributed to one file (spec/03 §3 step 3)."""

    path: Path
    message: str


@dataclass(frozen=True, slots=True)
class CollectionResult:
    records: list[TestRecord]
    errors: list[CollectionError]


def module_name_for(path: Path, rootdir: Path) -> str:
    """`velox_tests.<dotted.relpath.without.suffix>` (spec/03 §3 step 1).

    Path-derived so two `test_utils.py` files in different directories never collide — the
    entire content of pytest's `ImportPathMismatchError`, deleted rather than solved.
    Non-identifier characters in path segments are escaped so the result is always a legal
    dotted module name.
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
    if not escaped or escaped[0].isdigit():
        escaped = f"_{escaped}"
    return escaped


def collect(files: Iterable[Path], *, rootdir: Path) -> CollectionResult:
    """Import each file and build its records; `index` assigned once over the whole result.

    Per file, in the order given (spec/03 §3-4):

    1. Compute the module name (`module_name_for`) and import via `importlib.util`:
       `spec_from_file_location` → `module_from_spec` → insert into `sys.modules` →
       `exec_module`. An exception here becomes a `CollectionError`; the file contributes zero
       records and collection continues to the next file (spec/03 §3 step 3).
    2. Within the imported module, find `async def test_*` functions *defined* in it — i.e.
       `getattr(obj, "__module__", None) == module.__name__`, so a `test_*` helper imported from
       elsewhere isn't collected twice (spec/03 §4 step 1).
    3. Sort those by `func.__code__.co_firstlineno` — definition order, not `vars()` iteration
       order (spec/03 §4 step 2).
    4. Build one `TestRecord` per function, `id` as `"{path}::{qualname}"`.

    `files` is assumed already in deterministic order (`discover_files` gives you that); `index`
    is assigned across the concatenation of all files' records, in that order (spec/03 §4).
    """
    records: list[TestRecord] = []
    errors: list[CollectionError] = []
    index = 0

    for path in files:
        module_name = module_name_for(path, rootdir)
        try:
            module = _import_module(path, module_name)
        except Exception:
            # Attributed to the file, not raised: one broken test module must not take the
            # rest of the suite down with it (spec/03 §3 step 3).
            errors.append(CollectionError(path=path, message=traceback.format_exc()))
            continue

        functions = [
            obj for obj in vars(module).values() if _is_own_test_function(obj, module_name)
        ]
        functions.sort(key=lambda func: func.__code__.co_firstlineno)

        for func in functions:
            records.append(
                TestRecord(
                    id=f"{path}::{func.__qualname__}",
                    index=index,
                    path=path,
                    lineno=func.__code__.co_firstlineno,
                    qualname=func.__qualname__,
                    func=func,
                )
            )
            index += 1

    return CollectionResult(records=records, errors=errors)


def _import_module(path: Path, module_name: str) -> object:
    """`spec_from_file_location` → `module_from_spec` → `sys.modules` → `exec_module`.

    importlib-only, no `sys.path` insertion (spec/03 §3 step 2). Any exception during
    `exec_module` propagates to the caller, which turns it into a `CollectionError`; the
    half-initialized module is removed from `sys.modules` first so a later, unrelated import of
    the same dotted name can't observe it.
    """
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
    return module


def _is_own_test_function(obj: object, module_name: str) -> bool:
    """Whether `obj` is a collectible test: an `async def test_*` defined in this module."""
    return (
        inspect.iscoroutinefunction(obj)
        and getattr(obj, "__name__", "").startswith("test_")
        and getattr(obj, "__module__", None) == module_name
    )
