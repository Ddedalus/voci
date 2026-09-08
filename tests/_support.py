"""Plain factories for hand-built test data: TestRecords, and the fresh-event-loop-per-test
helper used by DI and FastAPI-layering tests. No pytest fixtures live here — see conftest.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from voci._collection.collect import TestRecord as Record
from voci._di.fixtures import ResolutionPlan
from voci._marks import Marks, decided, marks_of

#: A test with no `Depends(...)` at all still needs a plan (`_collect.py` gives every
#: `TestRecord` one, uniformly) — the trivial one, shared by every factory below.
EMPTY_PLAN = ResolutionPlan(steps=(), root_args=())


def make_record(
    index: int,
    func: Callable[..., object],
    qualname: str,
    plan: ResolutionPlan = EMPTY_PLAN,
    path: Path = Path("mod.py"),
    params: Mapping[str, object] | None = None,
    patches: tuple[str, ...] = (),
    marks: Marks | None = None,
) -> Record:
    """`marks` default to `func`'s own with their conditions decided, exactly as a collected
    record's are for a test with no per-case marks."""
    return Record(
        id=f"{path}::{qualname}",
        index=index,
        path=path,
        lineno=1,
        qualname=qualname,
        func=func,
        params=params,
        plan=plan,
        marks=decided(marks_of(func)) if marks is None else marks,
        patches=patches,
    )


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """Runs `coro` on a fresh event loop. `asyncio.run` also gives each test a fresh context."""
    return asyncio.run(coro)


@dataclass(frozen=True)
class Project:
    """A `tmp_path` wrapper for building a test project on disk."""

    root: Path

    def write(self, relpath: str, content: str) -> Path:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def write_pyproject(self, body: str) -> Path:
        return self.write("pyproject.toml", body)

    def write_passing_test(self, name: str = "test_ok.py") -> Path:
        return self.write(name, "async def test_ok():\n    pass\n")
