"""Configuration file loading: `[tool.velox]` in `pyproject.toml`.

`resolve` searches upward from the given paths for the `pyproject.toml` declaring `[tool.velox]`,
stopping at the git root, and turns that table into a validated `Config`. The directory holding it
becomes the run's rootdir. One file, one table — there is no inheritance and no per-directory
config.

Eight keys are recognized: `testpaths`, `concurrency`, `timeout`, `loop_watchdog`,
`test_file_patterns`, `ignore`, `env` and `filterwarnings`. Anything else is an error, as is a
value of the wrong type or shape. `cli.py` merges the resulting `Config` against the command line
and the built-in defaults.
"""

from __future__ import annotations

import os.path
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

__all__ = ["Config", "ConfigError", "resolve"]

_KNOWN_KEYS = frozenset(
    {
        "testpaths",
        "concurrency",
        "timeout",
        "loop_watchdog",
        "test_file_patterns",
        "ignore",
        "env",
        "filterwarnings",
    }
)


class ConfigError(Exception):
    """A `[tool.velox]` table (or the `pyproject.toml` containing it) that can't be used.

    A usage error at the CLI. velox never falls back to defaults silently when the given
    config can't be honored.
    """


@dataclass(frozen=True, slots=True)
class Config:
    """The result of one `resolve()` call: a rootdir, plus whatever `[tool.velox]` said.

    Every optional field is `None` (or, for `env`, empty) when either no `[tool.velox]` table was
    found at all or the table simply didn't set that key — `cli.py` is the one that knows what
    "unset" should fall back to for each; built-in defaults live there, not here.
    """

    #: Where `[tool.velox]` was found, or the common ancestor of the requested paths if it wasn't.
    #: Callers resolve relative config paths (`testpaths`) against this, not `cwd()`.
    rootdir: Path
    #: The `pyproject.toml` that supplied this config, or `None` if none was found — surfaced so
    #: `cli.py` can name it in the startup header.
    source: Path | None = None
    #: The nearest ancestor of the search start containing `.git`, noted on the same upward walk
    #: that looked for `[tool.velox]` — set even when `source` is `None`, unlike `rootdir`, which
    #: falls back to the search start itself in that case and so is no use as a fixed anchor.
    #: `None` when the walk never crossed a `.git` boundary, or crossed one only after already
    #: finding a table (that walk stops the moment it finds one, before checking any further).
    git_root: Path | None = None
    testpaths: tuple[str, ...] | None = None
    concurrency: int | None = None
    timeout: float | None = None
    #: Seconds the event loop may go unresponsive before velox names the call holding it;
    #: `0` switches that diagnostic off entirely.
    loop_watchdog: float | None = None
    test_file_patterns: tuple[str, ...] | None = None
    ignore: tuple[str, ...] | None = None
    #: `Mapping`, not `dict`: every other collection field here is a `tuple` for the same reason
    #: -- `frozen=True` only stops `config.env = ...`, not `config.env["X"] = "Y"` mutating a
    #: shared `dict` in place out from under whoever else holds this `Config`. `MappingProxyType`
    #: closes that gap the same way a `tuple` does for `testpaths`/`ignore`/`test_file_patterns`.
    env: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    #: Warning filter specs, lowest precedence first, in `-W`'s own
    #: `action:message:category:module:lineno` form. Only the shape is checked here: a spec names
    #: a warning category, and resolving one can import the suite's own code, which has no
    #: business happening while the config that says where that code lives is still being read.
    #: `cli.py` parses them once `rootdir` is on `sys.path`.
    filterwarnings: tuple[str, ...] | None = None


def resolve(explicit_paths: Sequence[Path]) -> Config:
    """Find the governing `pyproject.toml`, if any, and parse its `[tool.velox]` table.

    The rootdir search starts at the common ancestor of `explicit_paths` and walks upward,
    stopping as soon as a `pyproject.toml` declaring `[tool.velox]` is found. The walk also stops
    the moment it reaches a directory containing `.git` — checking that directory's own
    `pyproject.toml` first (the common case: a `.git` at the project root sitting next to the
    `pyproject.toml` that governs it), but never looking above it. A `pyproject.toml` with no
    `[tool.velox]` table (a package nested inside a bigger repo, say) is not a match; the walk
    continues past it.

    `explicit_paths` is `cli.main`'s `args.paths`, already known to exist — empty when the user
    gave none, in which case the search starts at `cwd()`.
    """
    start = _search_start(explicit_paths)
    current = start
    while True:
        pyproject_path = current / "pyproject.toml"
        if pyproject_path.is_file():
            table = _read_tool_velox_table(pyproject_path)
            if table is not None:
                return _parse(table, rootdir=current, source=pyproject_path)
        if (current / ".git").exists():
            return Config(rootdir=start, git_root=current)
        parent = current.parent
        if parent == current:
            # Filesystem root, reached without ever finding a `.git` boundary (a repo-less
            # checkout, or `explicit_paths` pointing somewhere outside any repo, e.g. a test's
            # `tmp_path`). Nothing left to search.
            break
        current = parent
    return Config(rootdir=start)


def _search_start(explicit_paths: Sequence[Path]) -> Path:
    """The directory the upward search begins at: the resolved common ancestor of
    `explicit_paths`, or `cwd()` if there are none.

    Each path contributes its own directory, not itself, when it names a file directly — a
    single `velox tests/test_x.py` must not make `tests/test_x.py` (a file, not a directory) the
    search start, which `commonpath` would otherwise treat as one more path component to match
    exactly, silently narrowing the search to files named identically at every level above.
    """
    if not explicit_paths:
        return Path.cwd().resolve()
    dirs: list[Path] = []
    for raw in explicit_paths:
        resolved = raw.resolve()
        dirs.append(resolved if resolved.is_dir() else resolved.parent)
    try:
        return Path(os.path.commonpath(dirs))
    except ValueError as exc:
        # `commonpath` raises when its inputs don't share a root at all (mixed drives on
        # Windows; unreachable on Linux/macOS, where every `.resolve()`d path shares `/`) -- a
        # `ConfigError` gives `cli.main` a clean usage error instead of an unhandled traceback.
        raise ConfigError(
            f"can't find a common directory for {[str(p) for p in explicit_paths]}: {exc}"
        ) from exc


def _read_tool_velox_table(pyproject_path: Path) -> dict[str, object] | None:
    """`[tool.velox]` from `pyproject_path`, or `None` if the file has no such table.

    A `pyproject.toml` that fails to parse at all is always an error, even when it turns out not
    to be the file that would have matched — a broken TOML file sitting between the search start
    and the real config is a real problem the user needs to know about, not something to silently
    step over on the way to a file that does parse.
    """
    try:
        with pyproject_path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{pyproject_path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{pyproject_path}: {exc}") from exc
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return None
    velox_table = tool.get("velox")
    if velox_table is None:
        return None
    if not isinstance(velox_table, dict):
        raise ConfigError(f"{pyproject_path}: [tool.velox] must be a table")
    return velox_table


def _parse(table: dict[str, object], *, rootdir: Path, source: Path) -> Config:
    unknown = set(table) - _KNOWN_KEYS
    if unknown:
        raise ConfigError(
            f"{source}: unknown [tool.velox] key(s): {', '.join(sorted(unknown))} "
            f"(known keys: {', '.join(sorted(_KNOWN_KEYS))})"
        )

    return Config(
        rootdir=rootdir,
        source=source,
        testpaths=_str_list(table.get("testpaths"), key="testpaths", source=source),
        concurrency=_concurrency(table.get("concurrency"), source=source),
        timeout=_timeout(table.get("timeout"), source=source),
        loop_watchdog=_seconds(table.get("loop_watchdog"), key="loop_watchdog", source=source),
        test_file_patterns=_str_list(
            table.get("test_file_patterns"), key="test_file_patterns", source=source
        ),
        ignore=_str_list(table.get("ignore"), key="ignore", source=source),
        env=_str_dict(table.get("env"), key="env", source=source),
        filterwarnings=_str_list(table.get("filterwarnings"), key="filterwarnings", source=source),
    )


def _concurrency(value: object, *, source: Path) -> int | None:
    """`[tool.velox] concurrency` must be a plain `int`. `bool` is rejected explicitly even
    though it's an `int` subclass: TOML's `true`/`false` silently becoming `1` would be a
    surprising coercion, not a real value the user wrote (`test_concurrency_must_be_a_plain_
    integer` pins this alongside the ordinary wrong-type cases).
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{source}: 'concurrency' must be an integer, got {value!r}")
    return value


def _timeout(value: object, *, source: Path) -> float | None:
    """`[tool.velox] timeout` must be an `int` or `float` -- same `bool` exclusion as
    `_concurrency`, for the same reason (`test_timeout_rejects_a_bool`). Always returned as
    `float`, matching `Config.timeout`'s own type regardless of which numeric form was written.
    """
    return _seconds(value, key="timeout", source=source)


def _seconds(value: object, *, key: str, source: Path) -> float | None:
    """One of the numeric, seconds-valued keys. Whether the number makes sense as a budget is
    `cli.py`'s to say, since the flag that overrides it needs the identical check anyway; this
    only rejects what isn't a number at all."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{source}: {key!r} must be a number, got {value!r}")
    return float(value)


def _str_list(value: object, *, key: str, source: Path) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{source}: '{key}' must be a list of strings, got {value!r}")
    return tuple(value)


def _str_dict(value: object, *, key: str, source: Path) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ConfigError(f"{source}: '{key}' must be a table of string to string, got {value!r}")
    return MappingProxyType(dict(value))
