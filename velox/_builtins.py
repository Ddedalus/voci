"""Built-in fixtures and the types they hand back.

These are ordinary `Fixture` objects, declared with the ordinary decorator — there is nothing
privileged about them except that the runtime supplies the value instead of calling the function.
The five free functions at the bottom of this module (`tmp_path`, `tmp_path_factory`, `capture`,
`log_records`, `test_info`) are `builtin_fixture(func, provider=...)`-built: `func`'s own body
stays `raise NotImplementedError(...)`, since `func` is never called once `provider` is set —
`_di._construct` checks `.provider` first. The real values come from `velox._capture`'s five
providers instead, wired in below.

The five types those providers hand back (`Capture`, `LogRecords`, `TmpPathFactory`, plus the
already-plain `TestInfo`) take generic constructor arguments (a `Sink`-shaped protocol, a live
`list[LogRecord]`, a bare `Path`) rather than importing `velox._capture`'s concrete types, so this
module never needs to know `_capture`'s own internals beyond the small structural shape each class
reads from — `_capture.py` is the one that imports these types to build them.

There is no `monkeypatch`: it would add API without adding capability, since the underlying write
is process-global either way. Use `unittest.mock` (which velox schedules solo) or a DI override.
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
    """The `request` replacement: read-only, and deliberately tiny."""

    id: str
    """`relative/path/test_file.py::test_name[param-id]`."""
    tags: tuple[str, ...]
    timeout: float | None
    """The suite's effective `--timeout` budget for this test, or `None` for no limit. Reflects
    what `_run._run_one` actually enforces, not a per-test `@velox.timeout(...)` mark override —
    reporting anything other than the value actually in force would be reporting something
    false."""
    worker: int
    """Which concurrency slot (`0..concurrency-1`) this test is occupying."""


class _CapturedText(Protocol):
    """The structural shape `Capture` needs from whatever holds the live captured text — a
    `velox._capture.Sink`, in practice, but named here as a `Protocol` rather than imported
    concretely so this module never has to depend on `_capture`'s own internals."""

    @property
    def out(self) -> str: ...
    @property
    def err(self) -> str: ...


@final
class Capture:
    """The current test's captured stdout/stderr, live during the test.

    Holds a reference to the test's `Sink`, not a snapshot: `.out`/`.err` read straight through to
    it on every access, so text written after this fixture was injected — including from code
    that runs later in the same test — is visible immediately, with no polling or buffering.
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
    """`level`, as an int `logging` understands — resolved and validated *now*, not deferred.

    `bool` is rejected even though `isinstance(True, int)` is `True` in Python: `set_level(True)`
    is almost certainly a mistake (`True == 1`, a level nothing in `logging` ever means), and
    silently accepting it as level `1` would be a confusing, hard-to-debug success rather than a
    clear failure.
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
class _LevelOverride(AbstractContextManager[None]):
    """`LogRecords.set_level`'s context manager: raises or lowers `logger`'s own explicit level
    for the block, then restores exactly what was there before — including `logging.NOTSET`,
    which means "inherit from my parent" and is a meaningfully different state from any concrete
    level, so it must be restored as `NOTSET` itself rather than resolved-and-reapplied.

    A hand-rolled `__enter__`/`__exit__` class, not `@contextlib.contextmanager`: the latter turns
    the decorated function into a generator whose body — including any validation — does not run
    until the first `next()`/`.send()`, which `__enter__` is what triggers. That defers a bad
    `level`/`logger` argument's error from "the moment `set_level(...)` is called" to "the moment
    `with ...:` is entered", which is the exact bug this class's caller (`LogRecords.set_level`)
    exists to avoid — see `tests/test_public_api.py`'s
    `test_log_records_set_level_raises_at_call_time_not_at_enter`. Validation itself happens in
    `set_level` before this object is even constructed; this class only does the save/restore.

    The "previous" level is snapshotted in `__enter__`, not in `__init__` — `set_level(...)`
    returns this object without entering it, so holding it and entering later (or not at all) is a
    supported shape, and snapshotting eagerly at construction time would pair the restore with
    whatever the level happened to be at `set_level(...)`-call time rather than at the moment this
    block actually took over, silently restoring a stale value if anything changed the logger's
    level in between.
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

    Wraps the live `list[logging.LogRecord]` `_capture._RoutingHandler.emit` appends to — not a
    copy — so `.records`/`.messages` reflect records logged after this fixture was injected,
    matching `Capture`'s own "live" contract.
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

        `logger=None` means the root logger — every logger without its own explicit level
        inherits from it (`Logger.getEffectiveLevel`'s walk up `.parent`), so this is the "just
        let me see DEBUG records for a bit" case most tests actually want.

        `level`/`logger` are validated *before* any context manager is constructed or entered —
        see `_resolve_level` and `_LevelOverride`'s docstrings for why that ordering is load-
        bearing, not incidental.

        Logger levels are process-global: this call under concurrency can change what a
        concurrent sibling captures for a logger of the same name, in either direction. See
        `docs/rationale.md` ("set_level's concurrency hazard") for which direction is safe and
        which silently drops a sibling's records.
        """
        resolved = _resolve_level(level)
        if logger is not None and not isinstance(logger, str):
            raise TypeError(f"set_level(logger={logger!r}): expected str or None")
        target = logging.getLogger(logger)
        return _LevelOverride(target, resolved)


@final
class TmpPathFactory:
    """Session-scoped temp directory factory: same numbered-root and retention policy as pytest.

    `mktemp` numbers by construction (a per-basename counter starting at `0`), never by scanning
    the directory for a free number and retrying: this factory is session-scoped, so two
    concurrently-running tests can both be holding it and calling `.mktemp(...)` at the same time.
    The counter itself needs no lock despite that: incrementing a plain `dict` entry has no
    `await` in it, and asyncio is single-threaded, so no rival task's own step can ever interleave
    between the read and the write.
    """

    __slots__ = ("_basetemp", "_counters")

    def __init__(self, basetemp: Path) -> None:
        self._basetemp = basetemp
        self._counters: dict[str, int] = {}

    def mktemp(self, basename: str, *, numbered: bool = True) -> Path:
        # Module-level `_capture` (bound at the bottom of this file, after this class is already
        # defined) rather than a top-of-file import: same import-cycle reasoning as the
        # bottom-of-file import below. Resolved at call time, not class-definition time, so by
        # the time anything can actually call `mktemp`, `_capture` is already bound.
        sanitized = _capture.sanitize_test_id(basename)
        if numbered:
            n = self._counters.get(sanitized, 0)
            self._counters[sanitized] = n + 1
            path = self._basetemp / f"{sanitized}{n}"
        else:
            path = self._basetemp / sanitized
        # `exist_ok=False`: for `numbered=True` this can never legitimately collide (the counter
        # above guarantees a fresh suffix every call); for `numbered=False` two calls with the
        # same `basename` colliding is a caller bug (asking for the same fixed directory twice)
        # that should surface as a clear `FileExistsError`, not silently hand back a directory a
        # previous call may still be using.
        path.mkdir(parents=True, exist_ok=False)
        return path

    def getbasetemp(self) -> Path:
        return self._basetemp


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
