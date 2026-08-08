"""Built-in fixtures and the types they hand back.

These are ordinary `Fixture` objects, declared with the ordinary decorator — there is nothing
privileged about them except that the runtime supplies the value instead of calling the function.
Their bodies are unreachable and say so.

There is deliberately no `monkeypatch`: it would add API without adding capability, since the
underlying write is process-global either way. Use `unittest.mock` (which velox schedules solo)
or a DI override.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from logging import LogRecord
from pathlib import Path
from typing import final

from velox._fixtures import fixture

__all__ = [
    "Capture",
    "LogRecords",
    "TestInfo",
    "TmpPathFactory",
    "capture",
    "log_records",
    "test_info",
    "tmp_path",
    "tmp_path_factory",
]

_RUNTIME = "provided by the velox runtime; not callable directly"


@final
@dataclass(frozen=True, slots=True)
class TestInfo:
    """The `request` replacement: read-only, and deliberately tiny."""

    id: str
    """`relative/path/test_file.py::test_name[param-id]`."""
    tags: tuple[str, ...]
    timeout: float | None
    worker: int
    """Which concurrency slot this test is occupying."""


@final
class Capture:
    """The current test's captured stdout/stderr (spec/09)."""

    __slots__ = ()

    @property
    def out(self) -> str:
        raise NotImplementedError(_RUNTIME)

    @property
    def err(self) -> str:
        raise NotImplementedError(_RUNTIME)


@final
class LogRecords:
    """The `caplog` equivalent: structured records captured for this test (spec/09)."""

    __slots__ = ()

    @property
    def records(self) -> Sequence[LogRecord]:
        raise NotImplementedError(_RUNTIME)

    @property
    def messages(self) -> Sequence[str]:
        raise NotImplementedError(_RUNTIME)

    def set_level(
        self, level: int | str, *, logger: str | None = None
    ) -> AbstractContextManager[None]:
        """Raise or lower a logger's level for the duration of the block, then restore it."""
        raise NotImplementedError(_RUNTIME)


@final
class TmpPathFactory:
    """Session-scoped temp directory factory: same numbered-root and retention policy as pytest."""

    __slots__ = ()

    def mktemp(self, basename: str, *, numbered: bool = True) -> Path:
        raise NotImplementedError(_RUNTIME)

    def getbasetemp(self) -> Path:
        raise NotImplementedError(_RUNTIME)


@fixture()
def tmp_path() -> Path:
    """A directory unique to this test *by construction*: `basetemp/<sanitized-test-id>`.

    No scan-and-retry for a free number — that is a serial-era artifact.
    """
    raise NotImplementedError(_RUNTIME)


@fixture(scope="session")
def tmp_path_factory() -> TmpPathFactory:
    raise NotImplementedError(_RUNTIME)


@fixture()
def capture() -> Capture:
    raise NotImplementedError(_RUNTIME)


@fixture()
def log_records() -> LogRecords:
    raise NotImplementedError(_RUNTIME)


@fixture()
def test_info() -> TestInfo:
    raise NotImplementedError(_RUNTIME)
