"""Stand-ins for everything the vendored pytest assertion code imports from pytest.

Hand-written, not vendored. This is the whole reason the vendoring is a small diff: the
rewriter and explanation engine touch pytest's `Config`, `Session`, `Stash`, path helpers and
`outcomes` only at the edges, so ~130 LOC of stubs replaces the several thousand lines of
pytest those imports would otherwise drag in.

Everything here is deliberately minimal and duck-typed. If a re-vendor makes the vendored code
reach for something new, `scripts/vendor_assertion.py` fails on an unmapped import rather than
silently importing pytest.
"""

from __future__ import annotations

import fnmatch
import os
import sys
import traceback
from pathlib import Path, PurePath
from typing import Any, NoReturn, Protocol

__all__ = [
    "AssertionState",
    "Config",
    "FixtureFunctionDefinition",
    "Session",
    "Stash",
    "StashKey",
    "absolutepath",
    "assert_never",
    "fnmatch_ex",
    "outcomes",
    "repr_crash",
    "running_on_ci",
    "version",
]

#: Bumped whenever the vendored codegen changes. Baked into the pyc cache tag (spec/07 §4.2),
#: so bumping it invalidates every cached rewritten module.
VELOX_REWRITER_REVISION = 1

#: What `rewrite.py` interpolates into its pyc tag. Deliberately not velox's package version:
#: a velox release that does not touch the rewriter should not throw away everyone's cache.
version = f"velox-r{VELOX_REWRITER_REVISION}"


# --------------------------------------------------------------------------- path helpers
# from _pytest/pathlib.py


def absolutepath(path: str | os.PathLike[str]) -> Path:
    """`Path.absolute()` without the symlink resolution `Path.resolve()` would do."""
    return Path(os.path.abspath(path))


def fnmatch_ex(pattern: str, path: str | os.PathLike[str]) -> bool:
    """`fnmatch` against the basename, or against the whole path if the pattern has a sep.

    Matches pytest's rule so that `python_files` patterns behave identically.
    """
    path = PurePath(path)
    iswin32 = sys.platform.startswith("win")

    if iswin32 and os.sep not in pattern and "/" in pattern:
        pattern = pattern.replace("/", os.sep)

    if os.sep not in pattern:
        name = path.name
    else:
        name = str(path)
        if path.is_absolute() and not os.path.isabs(pattern):
            pattern = f"*{os.sep}{pattern}"
    return fnmatch.fnmatch(name, pattern)


# --------------------------------------------------------------------------------- compat
# from _pytest/compat.py


def running_on_ci() -> bool:
    """CI suppresses truncation: nobody can scroll back through a CI log to see the rest."""
    return any(os.environ.get(var) for var in ("CI", "BUILD_NUMBER"))


def assert_never(value: NoReturn) -> NoReturn:
    raise AssertionError(f"Unhandled value: {value} ({type(value).__name__})")


# ---------------------------------------------------------------------------------- stash
# from _pytest/stash.py, minus the type gymnastics


class StashKey[T]:
    """Typed key into a `Stash`. Identity-keyed, so two keys never collide."""

    __slots__ = ()


class Stash:
    """Type-safe heterogeneous store, keyed by `StashKey` identity."""

    __slots__ = ("_storage",)

    def __init__(self) -> None:
        self._storage: dict[StashKey[Any], object] = {}

    def __setitem__[T](self, key: StashKey[T], value: T) -> None:
        self._storage[key] = value

    def __getitem__[T](self, key: StashKey[T]) -> T:
        return self._storage[key]  # type: ignore[return-value]

    def __contains__(self, key: StashKey[Any]) -> bool:
        return key in self._storage

    def get[T](self, key: StashKey[T], default: T) -> T:
        return self._storage.get(key, default)  # type: ignore[return-value]


# --------------------------------------------------------------------------------- config

#: Defaults for every ini option the vendored code reads. velox has no ini parser here; the
#: real values arrive from velox's own config layer (spec/02) when it constructs a `Config`.
_INI_DEFAULTS: dict[str, object] = {
    # Which modules the rewriter matches. velox widens this at the call site (spec/07 Q16).
    "python_files": ["test_*.py", "*_test.py"],
    # Changes generated code, so it is part of the pyc cache key. Off by default.
    "enable_assertion_pass_hook": False,
    # None means "use TruncationBudget's default"; 0 disables that dimension.
    "truncation_limit_lines": None,
    "truncation_limit_chars": None,
    "assertion_text_diff_style": "ndiff",
}


class Config:
    """The slice of pytest's `Config` the vendored code actually reads.

    Verbosity is a single number here; pytest supports per-kind overrides, which velox does
    not need — `VERBOSITY_ASSERTIONS` exists only so the vendored call sites still resolve.
    """

    VERBOSITY_ASSERTIONS = "assertions"

    __slots__ = ("_ini", "_verbosity", "stash")

    def __init__(
        self,
        ini: dict[str, object] | None = None,
        *,
        verbosity: int = 0,
    ) -> None:
        self._ini = {**_INI_DEFAULTS, **(ini or {})}
        self._verbosity = verbosity
        self.stash = Stash()

    def getini(self, name: str) -> Any:
        try:
            return self._ini[name]
        except KeyError:
            raise ValueError(f"unknown ini option: {name!r}") from None

    def get_verbosity(self, kind: str | None = None) -> int:
        return self._verbosity


class Session(Protocol):
    """The two things the rewriter's early-bailout path asks of a session.

    `_initialpaths` keeps the leading-underscore name because the vendored code uses it, and is
    declared read-only so an immutable implementation still satisfies the protocol.
    """

    @property
    def _initialpaths(self) -> frozenset[Path]: ...

    def isinitpath(self, path: Path) -> bool: ...


class FixtureFunctionDefinition:
    """Marker only.

    The rewriter checks `isinstance(obj, FixtureFunctionDefinition)` to avoid rendering a
    fixture's repr in an explanation. velox's fixtures are a different type entirely, so this
    never matches — which is the correct behaviour, just reached differently.
    """

    __slots__ = ()


class AssertionState:
    """Per-run rewriter state, stashed on the config.

    Upstream carries the pytest plugin hook here too; velox's hook lives in ContextVars
    instead (spec/07 §4.1), so this is just the mode plus tracing.
    """

    __slots__ = ("mode", "trace")

    def __init__(self, mode: str = "rewrite", trace: Any = None) -> None:
        self.mode = mode
        self.trace = trace if trace is not None else _no_trace


def _no_trace(msg: str) -> None:
    """Tracing is off by default; velox swaps in a real sink under `--debug` (spec/02)."""


# ------------------------------------------------------------------------------- outcomes


class _Outcomes:
    """Namespace matching `_pytest.outcomes` for the one attribute the engine uses.

    `assertrepr_compare` swallows every exception raised while building an explanation, except
    this one — a control-flow signal to abort the run must not be mistaken for a bad `__repr__`.
    """

    class Exit(Exception):
        """Raised to end the run immediately, with no traceback or summary."""

        def __init__(self, msg: str = "unknown reason", returncode: int | None = None) -> None:
            self.msg = msg
            self.returncode = returncode
            super().__init__(msg)

    def __repr__(self) -> str:
        return "<velox assertion shim: outcomes>"


outcomes = _Outcomes()

Exit = _Outcomes.Exit


def repr_crash() -> str:
    """One-line `path:lineno: ExcType: message` for the exception being handled.

    Replaces `_pytest._code.ExceptionInfo.from_current()._getreprcrash()`, which is the tip of
    a large traceback subsystem the explanation engine reaches for on exactly one failure path:
    an object whose `__repr__` blew up while a diff was being rendered.
    """
    exc = sys.exc_info()[1]
    if exc is None:
        return "no active exception"
    tb = traceback.extract_tb(exc.__traceback__)
    where = f"{tb[-1].filename}:{tb[-1].lineno}: " if tb else ""
    return f"{where}{type(exc).__name__}: {exc}"
