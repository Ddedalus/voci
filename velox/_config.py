"""Configuration file loading: `[tool.velox]` in `pyproject.toml` (spec/02 §3) — M1 slice.

One file, one table, no inheritance, no per-directory config (spec/02 §3's own opening line).
This module owns exactly two things: finding *which* `pyproject.toml` governs a run (the rootdir
search) and turning its `[tool.velox]` table into a validated `Config`. It does not apply any of
it — `cli.py` merges `Config` against CLI args (CLI wins) and built-in defaults, because the
precedence rule (spec/02 §3: "CLI > environment > `[tool.velox]` > built-in defaults") is a
property of *all three* tiers together, not of this module alone. The `VELOX_*` environment tier
in that chain is not implemented here — nothing in the shipped examples uses it yet, and it's a
separate, independently-scoped surface; see spec/02 §2's option tables for the names it would use.

Only the keys the shipped examples (`examples/01-fastapi-crud`, `examples/02-async-library`)
actually need are recognized: `testpaths`, `concurrency`, `timeout`, `test_file_patterns`,
`ignore`, `env` — every one of spec/02 §3's example table *except* `watchdog_threshold`, which has
no consumer yet (no watchdog exists before M2) and is left for whichever M2 slice builds one.
Recognizing a key this module can't make do anything would be exactly the "typo'd key silently
does nothing" footgun spec/02 §3 says `[tool.velox]` must not have (enforced below by rejecting
anything outside that set, not just those six).
"""

from __future__ import annotations

import os.path
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

__all__ = ["Config", "ConfigError", "resolve"]

#: spec/02 §3's example table, minus `watchdog_threshold` — see the module docstring for why.
_KNOWN_KEYS = frozenset(
    {"testpaths", "concurrency", "timeout", "test_file_patterns", "ignore", "env"}
)


class ConfigError(Exception):
    """A `[tool.velox]` table (or the `pyproject.toml` containing it) that can't be used.

    Always a usage error at the CLI (spec/02 §4, exit 4) — never a reason to fall back to
    defaults silently. A config the user wrote that velox can't honor is exactly the "escalate,
    never silently degrade" case (I6): guessing what they meant would risk running the wrong
    tests with the wrong settings and calling it success.
    """


@dataclass(frozen=True, slots=True)
class Config:
    """The result of one `resolve()` call: a rootdir, plus whatever `[tool.velox]` said.

    Every optional field is `None` (or, for `env`, empty) when either no `[tool.velox]` table was
    found at all or the table simply didn't set that key — `cli.py` is the one that knows what
    "unset" should fall back to for each (built-in defaults live there, not here, so this module
    stays ignorant of e.g. `_run.DEFAULT_CONCURRENCY`).
    """

    #: Where `[tool.velox]` was found, or the common ancestor of the requested paths (spec/02 §3:
    #: "if none is found, the common ancestor itself is the rootdir and defaults apply") if it
    #: wasn't. Callers resolve relative config paths (`testpaths`) against this, not `cwd()`.
    rootdir: Path
    #: The `pyproject.toml` that supplied this config, or `None` if none was found — surfaced so
    #: `cli.py` can name it in the startup header, the same transparency `_rewrite.plan`'s own
    #: header line already gives the assertion-rewrite decision.
    source: Path | None = None
    testpaths: tuple[str, ...] | None = None
    concurrency: int | None = None
    timeout: float | None = None
    test_file_patterns: tuple[str, ...] | None = None
    ignore: tuple[str, ...] | None = None
    #: `Mapping`, not `dict`: every other collection field here is a `tuple` for the same
    #: reason -- `frozen=True` only stops `config.env = ...`, not `config.env["X"] = "Y"` mutating
    #: a shared `dict` in place out from under whoever else holds this `Config`. `MappingProxyType`
    #: closes that gap the same way a `tuple` does for `testpaths`/`ignore`/`test_file_patterns`.
    env: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


def resolve(explicit_paths: Sequence[Path]) -> Config:
    """Find the governing `pyproject.toml`, if any, and parse its `[tool.velox]` table.

    spec/02 §3: rootdir search starts at "the common ancestor of PATHS" and walks upward,
    stopping as soon as a `pyproject.toml` declaring `[tool.velox]` is found. Per the spec's own
    review note ("pay attention to not cause carnage when traversing the path up to root ...
    should stop at git root"), the walk also stops the moment it reaches a directory containing
    `.git` — checking that directory's own `pyproject.toml` first (the common case: a `.git` at
    the project root sitting next to the `pyproject.toml` that governs it), but never looking
    above it. A `pyproject.toml` with no `[tool.velox]` table (a package nested inside a bigger
    repo, say) is not a match; the walk continues past it.

    `explicit_paths` is `cli.main`'s `args.paths`, already known to exist (`cli._invalid_path_
    argument` runs first) — empty when the user gave none, in which case the search starts at
    `cwd()`, matching "the common ancestor of PATHS" degenerating to "here" when there is no
    PATHS.
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
            break
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
    `explicit_paths`, or `cwd()` if there are none (spec/02 §3's "if PATHS is empty").

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
        # Windows; on the Linux/macOS targets spec/00 §2 actually commits to, every `.resolve()`d
        # path shares `/`, so this is unreachable there) -- a `ConfigError` gives `cli.main` a
        # clean exit-4 usage error instead of an unhandled traceback (I6).
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
        test_file_patterns=_str_list(
            table.get("test_file_patterns"), key="test_file_patterns", source=source
        ),
        ignore=_str_list(table.get("ignore"), key="ignore", source=source),
        env=_str_dict(table.get("env"), key="env", source=source),
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
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{source}: 'timeout' must be a number, got {value!r}")
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
