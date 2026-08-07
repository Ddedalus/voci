"""Assertion helpers.

The primary assertion mechanism is plain `assert`, rewritten for introspection (spec/07). These
two cover what `assert` alone cannot express.

Unlike the rest of the package, these are implemented rather than stubbed: they are pure, they
depend on nothing in the runtime, and having them work makes the examples readable.
"""

from __future__ import annotations

import re
from math import isclose, isnan
from types import TracebackType
from typing import final

__all__ = ["Approx", "ExceptionInfo", "RaisesContext", "approx", "raises"]

type ExcTypes = type[BaseException] | tuple[type[BaseException], ...]


@final
class ExceptionInfo[E: BaseException]:
    """Handle on the exception a `raises` block caught. Populated on block exit."""

    __slots__ = ("_exc",)

    def __init__(self) -> None:
        self._exc: E | None = None

    @property
    def value(self) -> E:
        if self._exc is None:
            raise AttributeError("the raises() block has not completed yet")
        return self._exc

    @property
    def type(self) -> type[E]:
        return type(self.value)

    @property
    def traceback(self) -> TracebackType | None:
        return self.value.__traceback__

    def match(self, pattern: str | re.Pattern[str]) -> bool:
        """`re.search` the string form of the exception. Returns True or raises AssertionError."""
        if re.search(pattern, str(self.value)) is None:
            raise AssertionError(f"pattern {pattern!r} does not match {str(self.value)!r}")
        return True

    def __repr__(self) -> str:
        return f"<ExceptionInfo {self._exc!r}>"


@final
class RaisesContext[E: BaseException]:
    """Context manager returned by `raises`."""

    __slots__ = ("_expected", "_info", "_match")

    def __init__(
        self, expected: type[E] | tuple[type[E], ...], match: str | re.Pattern[str] | None
    ) -> None:
        self._expected = expected
        self._match = match
        self._info: ExceptionInfo[E] = ExceptionInfo()

    def __enter__(self) -> ExceptionInfo[E]:
        return self._info

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object
    ) -> bool:
        __tracebackhide__ = True
        if exc is None:
            expected = getattr(self._expected, "__name__", repr(self._expected))
            raise AssertionError(f"DID NOT RAISE {expected}")
        if not isinstance(exc, self._expected):
            return False
        self._info._exc = exc
        if self._match is not None:
            self._info.match(self._match)
        return True


def raises[E: BaseException](
    expected: type[E] | tuple[type[E], ...],
    *,
    match: str | re.Pattern[str] | None = None,
) -> RaisesContext[E]:
    """Assert that the block raises `expected`, optionally with a message matching `match`.

    `match` is an `re.search`, not a full match — pytest-compatible, including the gotcha that
    regex metacharacters in a literal message need escaping.
    """
    return RaisesContext(expected, match)


@final
class Approx:
    """Tolerant numeric comparison. Compare with `==` in either direction."""

    __slots__ = ("_abs", "_expected", "_nan_ok", "_rel")

    DEFAULT_REL = 1e-6
    DEFAULT_ABS = 1e-12

    def __init__(
        self, expected: complex, *, rel: float | None, abs: float | None, nan_ok: bool
    ) -> None:
        self._expected = expected
        self._rel = rel
        self._abs = abs
        self._nan_ok = nan_ok

    def __eq__(self, actual: object) -> bool:
        if not isinstance(actual, int | float | complex) or isinstance(actual, bool):
            return NotImplemented
        if isinstance(self._expected, complex) or isinstance(actual, complex):
            return abs(complex(actual) - complex(self._expected)) <= (self._abs or self.DEFAULT_ABS)
        expected = float(self._expected)
        if isnan(expected) or isnan(float(actual)):
            return self._nan_ok and isnan(expected) and isnan(float(actual))
        return isclose(
            float(actual),
            expected,
            rel_tol=self.DEFAULT_REL if self._rel is None else self._rel,
            abs_tol=self.DEFAULT_ABS if self._abs is None else self._abs,
        )

    def __hash__(self) -> int:
        return hash(("velox.approx", self._expected))

    def __repr__(self) -> str:
        tolerance = f"rel={self._rel!r}, abs={self._abs!r}" if (self._rel or self._abs) else "±1e-6"
        return f"approx({self._expected!r} {tolerance})"


def approx(
    expected: complex,
    *,
    rel: float | None = None,
    abs: float | None = None,
    nan_ok: bool = False,
) -> Approx:
    """`assert value == velox.approx(0.3)`.

    MVP: scalars only. Sequences, mappings, and numpy arrays are roadmap.
    """
    return Approx(expected, rel=rel, abs=abs, nan_ok=nan_ok)
