"""Built-in fixtures and the types they hand back.

`tmp_path`, `tmp_path_factory`, `tmpdir`, `tmpdir_factory`, `capture`, `log_records` and
`test_info` are declared here as ordinary `Fixture` objects carrying a `provider`: the runtime
calls that provider instead of the decorated function, and the providers themselves live in
`velox._builtins.capture`, wired in at the bottom of this module.

The types those providers construct are defined here too — `Capture`, `LogRecords`,
`TmpPathFactory`, `LegacyPath`, `LegacyTmpPathFactory` and `TestInfo` — each taking plain
constructor arguments (a `Sink`-shaped protocol, a live `list[LogRecord]`, a `Path`).
"""

from __future__ import annotations

import logging
from collections.abc import MutableSequence, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, final

from velox._di.fixtures import Depends, builtin_fixture, fixture

__all__ = [
    "Capture",
    "LegacyPath",
    "LegacyTmpPathFactory",
    "LogRecords",
    "TestInfo",
    "TmpPathFactory",
    "capture",
    "log_records",
    "test_info",
    "tmp_path",
    "tmp_path_factory",
    "tmpdir",
    "tmpdir_factory",
]

_RUNTIME = "provided by the velox runtime; not callable directly"


@final
@dataclass(frozen=True, slots=True)
class TestInfo:
    """Read-only facts about the test that is running."""

    id: str
    """`relative/path/test_file.py::test_name[param-id]`."""
    tags: tuple[str, ...]
    """Every name attached to this test by `@velox.tag(...)`."""
    timeout: float | None
    """The budget this test is actually held to, or `None` for no limit: a per-test
    `@velox.timeout(...)` mark if it carries one, else the suite's `--timeout`."""
    worker: int
    """Which concurrency slot (`0..concurrency-1`) this test is occupying."""


class _CapturedText(Protocol):
    """The structural shape `Capture` needs from whatever holds the live captured text: a
    `velox._builtins.capture.Sink`, in practice."""

    @property
    def out(self) -> str: ...
    @property
    def err(self) -> str: ...


@final
class Capture:
    """The current test's captured stdout and stderr.

    A live view, not a snapshot: `.out`/`.err` read the capture buffers on every access, so text
    written after this fixture was injected is visible immediately.
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


#: pytest's `DEFAULT_LOG_FORMAT`, kept identical so a formatted line reads the same either way.
_LOG_FORMAT = "%(levelname)-8s %(name)s:%(filename)s:%(lineno)d %(message)s"


@final
class LogRecords:
    """The `logging` records captured for the current test.

    A live view, not a copy: `.records`/`.messages`/`.text`/`.record_tuples` reflect records
    logged after this fixture was injected, and `.clear()` empties the container the capture
    handler goes on appending to.
    """

    __slots__ = ("_records",)

    def __init__(self, records: MutableSequence[logging.LogRecord]) -> None:
        self._records = records

    @property
    def records(self) -> Sequence[logging.LogRecord]:
        return tuple(self._records)

    @property
    def messages(self) -> Sequence[str]:
        return tuple(record.getMessage() for record in self._records)

    @property
    def text(self) -> str:
        """Every captured record formatted, one per line."""
        formatter = logging.Formatter(_LOG_FORMAT)
        return "".join(formatter.format(record) + "\n" for record in self._records)

    @property
    def record_tuples(self) -> Sequence[tuple[str, int, str]]:
        """`(logger name, level, message)` for each captured record, for assertion comparison."""
        return tuple((record.name, record.levelno, record.getMessage()) for record in self._records)

    def clear(self) -> None:
        """Empty the captured records. A record logged after this call is captured as normal."""
        self._records.clear()

    def set_level(
        self, level: int | str, *, logger: str | None = None
    ) -> AbstractContextManager[None]:
        """Raise or lower a logger's level for the duration of the block, then restore it.

        `logger=None` targets the root logger, which every logger without its own explicit
        level inherits from (`Logger.getEffectiveLevel`'s walk up `.parent`). `level` and
        `logger` are validated before the context manager is constructed or entered.

        Logger levels are process-global, so a concurrent test logging to a logger of the same
        name is affected too: raising a level lets it capture more than it asked for, and
        lowering one can leave its own `set_level` block empty.
        """
        resolved = _resolve_level(level)
        if logger is not None and not isinstance(logger, str):
            raise TypeError(f"set_level(logger={logger!r}): expected str or None")
        target = logging.getLogger(logger)
        return _LevelOverride(target, resolved)


@final
class TmpPathFactory:
    """Session-scoped factory for temporary directories under the run's basetemp root.

    `mktemp` numbers from a per-basename counter rather than by scanning for a free number, so
    two tests calling it concurrently never collide.
    """

    __slots__ = ("_basetemp", "_counters")

    def __init__(self, basetemp: Path) -> None:
        self._basetemp = basetemp
        self._counters: dict[str, int] = {}

    def mktemp(self, basename: str, *, numbered: bool = True) -> Path:
        """Create and return a fresh directory under the basetemp root.

        `numbered` appends a per-`basename` counter, so repeated calls with one basename each
        get their own directory; `numbered=False` uses `basename` as given and raises
        `FileExistsError` if that directory is already there.
        """
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
        """The root directory every path this factory hands out lives under."""
        return self._basetemp


@final
class LegacyPath:
    """A `pathlib.Path` wrapped in the `py.path.local` surface pytest's own `tmpdir` hands out:
    `.join`, `.strpath`, `/`, `.write` and `.mkdir`.

    Any other attribute resolves on the wrapped `Path`, so one the two types share (`.exists()`,
    `.read_text()`) behaves as `Path`'s and one that only `py.path.local` had raises
    `AttributeError`.
    """

    __slots__ = ("_path",)

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def strpath(self) -> str:
        return str(self._path)

    def join(self, *args: str) -> LegacyPath:
        return LegacyPath(self._path.joinpath(*args))

    def mkdir(self, *args: str) -> LegacyPath:
        """Create and return the directory `.join(*args)` names.

        Shadows `Path.mkdir`, whose `mode`/`parents`/`exist_ok` keyword arguments this class
        does not carry over.
        """
        made = self.join(*args)
        made._path.mkdir()
        return made

    def write(self, data: str | bytes, mode: str = "w", *, ensure: bool = False) -> None:
        """Write `data` to the path in `mode`, creating parent directories first if `ensure`.

        `mode` is `open()`'s own mode string -- `"w"` truncates, `"a"` appends, a `"b"` in it
        picks binary -- and `data`'s type has to agree with it, exactly as `py.path.local.write`
        requires.
        """
        if ensure:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        binary = "b" in mode
        if binary and not isinstance(data, bytes):
            raise TypeError(f"write(mode={mode!r}): expected bytes, got {type(data).__name__}")
        if not binary and not isinstance(data, str):
            raise TypeError(f"write(mode={mode!r}): expected str, got {type(data).__name__}")
        with self._path.open(mode) as f:
            f.write(data)

    def __truediv__(self, other: str) -> LegacyPath:
        return LegacyPath(self._path / other)

    def __fspath__(self) -> str:
        return str(self._path)

    def __str__(self) -> str:
        return str(self._path)

    def __repr__(self) -> str:
        return f"LegacyPath({self._path!r})"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._path, name)


@final
class LegacyTmpPathFactory:
    """`TmpPathFactory`, handing back `LegacyPath` instead of `Path`."""

    __slots__ = ("_factory",)

    def __init__(self, factory: TmpPathFactory) -> None:
        self._factory = factory

    def mktemp(self, basename: str, *, numbered: bool = True) -> LegacyPath:
        return LegacyPath(self._factory.mktemp(basename, numbered=numbered))

    def getbasetemp(self) -> LegacyPath:
        return LegacyPath(self._factory.getbasetemp())


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


# Rewire the five fixtures above onto `velox._builtins.capture`'s providers, turning each from a
# `func`-raises-`NotImplementedError` stub into a runtime-supplied builtin fixture.
#
# Imported here, at the bottom of the module, not at the top: `capture.py` imports the classes
# above from this module, creating a genuine import cycle. It resolves cleanly because every name
# `capture.py` needs is already defined by the time control reaches this line — see
# plans/rationale.md ("_builtins/fixtures.py / _builtins/capture.py import cycle") for the full
# shape of it.
from velox._builtins import capture as _capture  # noqa: E402

tmp_path = builtin_fixture(tmp_path, provider=_capture.tmp_path_provider)
tmp_path_factory = builtin_fixture(tmp_path_factory, provider=_capture.tmp_path_factory_provider)
capture = builtin_fixture(capture, provider=_capture.capture_provider)
log_records = builtin_fixture(log_records, provider=_capture.log_records_provider)
test_info = builtin_fixture(test_info, provider=_capture.test_info_provider)


# `tmpdir`/`tmpdir_factory` are declared only now, `Depends()`-ing on the just-rebound `tmp_path`/
# `tmp_path_factory` rather than allocating a directory of their own: a test asking for both gets
# the same directory either way, and `tmpdir_factory.mktemp(...)` shares `tmp_path_factory`'s own
# numbering instead of starting a second counter over the same `basetemp`.
@fixture()
def tmpdir(tmp_path: Path = Depends(tmp_path)) -> LegacyPath:
    """This test's `tmp_path`, wrapped as a `LegacyPath`."""
    raise NotImplementedError(_RUNTIME)


@fixture(scope="session")
def tmpdir_factory(
    tmp_path_factory: TmpPathFactory = Depends(tmp_path_factory),
) -> LegacyTmpPathFactory:
    """The session's `tmp_path_factory`, wrapped as a `LegacyTmpPathFactory`."""
    raise NotImplementedError(_RUNTIME)


tmpdir = builtin_fixture(tmpdir, provider=_capture.tmpdir_provider)
tmpdir_factory = builtin_fixture(tmpdir_factory, provider=_capture.tmpdir_factory_provider)
