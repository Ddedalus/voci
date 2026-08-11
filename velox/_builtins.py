"""Built-in fixtures and the types they hand back.

`tmp_path`, `tmp_path_factory`, `capture`, `log_records` and `test_info` are declared here as
ordinary `Fixture` objects carrying a `provider`: the runtime calls that provider instead of the
decorated function, and the providers themselves live in `velox._capture`, wired in at the bottom
of this module.

The types those providers construct are defined here too — `Capture`, `LogRecords`,
`TmpPathFactory` and `TestInfo` — each taking plain constructor arguments (a `Sink`-shaped
protocol, a live `list[LogRecord]`, a `Path`).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, final

from velox._fixtures import builtin_fixture, fixture

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
    """The `request` replacement: read-only and small."""

    id: str
    """`relative/path/test_file.py::test_name[param-id]`."""
    tags: tuple[str, ...]
    timeout: float | None
    """The budget this test is actually held to, or `None` for no limit: a per-test
    `@velox.timeout(...)` mark if it carries one, else the suite's `--timeout`."""
    worker: int
    """Which concurrency slot (`0..concurrency-1`) this test is occupying."""


class _CapturedText(Protocol):
    """The structural shape `Capture` needs from whatever holds the live captured text: a
    `velox._capture.Sink`, in practice."""

    @property
    def out(self) -> str: ...
    @property
    def err(self) -> str: ...


@final
class Capture:
    """The current test's captured stdout/stderr, live during the test.

    Holds a reference to the test's `Sink`, not a snapshot: `.out`/`.err` read straight through
    on every access, so text written after this fixture was injected is visible immediately.
    """

    __slots__ = ("_source",)

    def __init__(self, source: _CapturedText) -> None:
        self._source = source

    @property
    def out(self) -> str:
        return self._source.out

    @property
    def err(self) -> str:
        return self._source.err


def _resolve_level(level: int | str) -> int:
    """`level`, as an int `logging` understands, resolved and validated immediately.

    `bool` is rejected even though `isinstance(True, int)` is `True`: `set_level(True)` is
    almost certainly a mistake, and silently treating it as level `1` would be a confusing
    success rather than a clear failure.
    """
    if isinstance(level, bool):
        raise TypeError(f"set_level(level={level!r}): bool is not a valid logging level")
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        resolved = logging.getLevelNamesMapping().get(level.upper())
        if resolved is None:
            raise ValueError(
                f"set_level(level={level!r}): unknown logging level name -- expected one of "
                f"{sorted(logging.getLevelNamesMapping())} or an int"
            )
        return resolved
    raise TypeError(f"set_level(level={level!r}): expected int or str, got {type(level).__name__}")


@final
# Hand-rolled __enter__/__exit__ rather than @contextlib.contextmanager, so entering never
# reruns a generator body; `set_level` does its own validation before this is constructed.
class _LevelOverride(AbstractContextManager[None]):
    """`LogRecords.set_level`'s context manager: raises or lowers `logger`'s own explicit level
    for the block, then restores exactly what was there before, including `logging.NOTSET`.

    The previous level is snapshotted in `__enter__`, not in `__init__`, so `set_level(...)` can
    return this object, be held, and be entered later without pairing the restore to a level
    that may have since changed.
    """

    __slots__ = ("_level", "_logger", "_previous")

    def __init__(self, logger: logging.Logger, level: int) -> None:
        self._logger = logger
        self._level = level
        self._previous: int | None = None

    def __enter__(self) -> None:
        self._previous = self._logger.level
        self._logger.setLevel(self._level)

    def __exit__(self, *exc_info: object) -> None:
        assert self._previous is not None, "__exit__ without a matching __enter__"
        self._logger.setLevel(self._previous)


@final
class LogRecords:
    """The `caplog` equivalent: structured records captured for this test.

    Wraps the live `list[logging.LogRecord]` that `_capture._RoutingHandler.emit` appends to,
    not a copy, so `.records`/`.messages` reflect records logged after this fixture was
    injected.
    """

    __slots__ = ("_records",)

    def __init__(self, records: Sequence[logging.LogRecord]) -> None:
        self._records = records

    @property
    def records(self) -> Sequence[logging.LogRecord]:
        return tuple(self._records)

    @property
    def messages(self) -> Sequence[str]:
        return tuple(record.getMessage() for record in self._records)

    def set_level(
        self, level: int | str, *, logger: str | None = None
    ) -> AbstractContextManager[None]:
        """Raise or lower a logger's level for the duration of the block, then restore it.

        `logger=None` targets the root logger, which every logger without its own explicit
        level inherits from (`Logger.getEffectiveLevel`'s walk up `.parent`). `level` and
        `logger` are validated before the context manager is constructed or entered.

        Logger levels are process-global, so this call under concurrency can change what a
        concurrent sibling captures for a logger of the same name. See `docs/rationale.md` for
        which direction is safe.
        """
        resolved = _resolve_level(level)
        if logger is not None and not isinstance(logger, str):
            raise TypeError(f"set_level(logger={logger!r}): expected str or None")
        target = logging.getLogger(logger)
        return _LevelOverride(target, resolved)


@final
class TmpPathFactory:
    """Session-scoped temp directory factory, sharing pytest's numbered-root and retention
    policy.

    `mktemp` numbers by construction, a per-basename counter starting at `0`, rather than
    scanning the directory for a free number — so two tests calling `.mktemp(...)`
    concurrently never collide.
    """

    __slots__ = ("_basetemp", "_counters")

    def __init__(self, basetemp: Path) -> None:
        self._basetemp = basetemp
        self._counters: dict[str, int] = {}

    def mktemp(self, basename: str, *, numbered: bool = True) -> Path:
        # `_capture` is bound at the bottom of this module, after this class is defined (see the
        # import-cycle note there); by the time anything can call `mktemp`, it's already bound.
        sanitized = _capture.sanitize_test_id(basename)
        if numbered:
            n = self._counters.get(sanitized, 0)
            self._counters[sanitized] = n + 1
            path = self._basetemp / f"{sanitized}{n}"
        else:
            path = self._basetemp / sanitized
        # `exist_ok=False`: numbered calls never collide (the counter guarantees a fresh
        # suffix); an unnumbered collision on the same `basename` is a caller bug that should
        # raise `FileExistsError`, not silently hand back a directory a previous call may still
        # be using.
        path.mkdir(parents=True, exist_ok=False)
        return path

    def getbasetemp(self) -> Path:
        return self._basetemp


@fixture()
def tmp_path() -> Path:
    """A directory unique to this test, by construction: `basetemp/<sanitized-test-id>`."""
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


# Rewire the five fixtures above onto `velox._capture`'s providers, turning each from a
# `func`-raises-`NotImplementedError` stub into a runtime-supplied builtin fixture.
#
# Imported here, at the bottom of the module, not at the top: `_capture` imports the classes
# above from this module, creating a genuine import cycle. It resolves cleanly because every name
# `_capture.py` needs is already defined by the time control reaches this line — see
# docs/rationale.md ("_builtins/_capture import cycle") for the full shape of it.
from velox import _capture  # noqa: E402

tmp_path = builtin_fixture(tmp_path.func, provider=_capture.tmp_path_provider, scope="function")
tmp_path_factory = builtin_fixture(
    tmp_path_factory.func, provider=_capture.tmp_path_factory_provider, scope="session"
)
capture = builtin_fixture(capture.func, provider=_capture.capture_provider, scope="function")
log_records = builtin_fixture(
    log_records.func, provider=_capture.log_records_provider, scope="function"
)
test_info = builtin_fixture(test_info.func, provider=_capture.test_info_provider, scope="function")
