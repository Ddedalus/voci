"""`raises` and `approx`: the two assertion helpers velox provides.

`raises` is a context manager that catches an expected exception and exposes it as
`ExceptionInfo`, optionally matching the exception type and a regex against its message.
`approx` wraps a number, or a collection of numbers, for tolerant `==` comparison.
"""

from __future__ import annotations

import asyncio
import re
from math import isclose, isnan
from types import NotImplementedType, TracebackType
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
        # RuntimeError, not AttributeError: AttributeError from a property is swallowed by
        # `hasattr`, `getattr(info, "value", default)`, and most repr/debug machinery, so a test
        # that reads `.value` too early would see a silent default instead of this error.
        if self._exc is None:
            raise RuntimeError("the raises() block has not completed yet")
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
        # `expected` is bounded by BaseException, so without this check `raises(SystemExit)`'s
        # sibling `raises(asyncio.CancelledError)` (or `raises(BaseException)`) would swallow the
        # cancellation velox's own timeout machinery uses to stop a runaway test, making that test
        # un-timeout-able. SystemExit and KeyboardInterrupt are unaffected — testing a CLI's
        # SystemExit is legitimate and common.
        types = expected if isinstance(expected, tuple) else (expected,)
        if any(issubclass(asyncio.CancelledError, t) for t in types):
            raise TypeError(
                "raises() cannot catch asyncio.CancelledError: velox uses cancellation to "
                "enforce test timeouts, and a raises() block that swallows it would make that "
                "test un-timeout-able."
            )
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

    def _tolerances(self) -> tuple[float, float]:
        """`(rel_tol, abs_tol)`, applying pytest's rule for an `abs`-only comparison.

        Naming `abs` without `rel` means *only* the absolute tolerance applies. Combining them the
        way `isclose` does — loosest wins — would let the 1e-6 relative default swallow the
        tolerance the caller actually asked for: `approx(1.0, abs=1e-13)` would accept a value off
        by 1e-7, and `approx(1.0, abs=0)` ("exact") would be no stricter than the default. Naming
        `rel` alone keeps `DEFAULT_ABS` underneath it, which is what makes comparisons against
        zero work at all.
        """
        abs_tol = self.DEFAULT_ABS if self._abs is None else self._abs
        if self._rel is None and self._abs is not None:
            return 0.0, abs_tol
        return (self.DEFAULT_REL if self._rel is None else self._rel), abs_tol

    def __eq__(self, actual: object) -> bool | NotImplementedType:
        if not isinstance(actual, int | float | complex) or isinstance(actual, bool):
            return NotImplemented
        rel_tol, abs_tol = self._tolerances()
        if isinstance(self._expected, complex) or isinstance(actual, complex):
            expected_c = complex(self._expected)
            actual_c = complex(actual)
            # Same "or" as `isclose`: whichever tolerance is looser wins, scaled by the larger
            # magnitude so it stays symmetric in both comparison directions.
            tolerance = max(rel_tol * max(abs(expected_c), abs(actual_c)), abs_tol)
            return abs(actual_c - expected_c) <= tolerance
        expected = float(self._expected)
        if isnan(expected) or isnan(float(actual)):
            return self._nan_ok and isnan(expected) and isnan(float(actual))
        return isclose(float(actual), expected, rel_tol=rel_tol, abs_tol=abs_tol)

    # A tolerant `__eq__` cannot have a consistent hash (`1.0 == approx(1.0)` is True but the two
    # would hash differently), so any hash we gave it would be a lie: `{approx(1.0): "x"}[1.0]`
    # would raise KeyError, and `approx(1.0) in {1.0}` would be False. Unhashable, like pytest's
    # ApproxBase, so the TypeError is loud instead of a container quietly losing the key.
    __hash__ = None  # pyrefly: ignore[bad-assignment]

    def __repr__(self) -> str:
        if self._rel is None and self._abs is None:
            tolerance = f"±{self.DEFAULT_REL!r}"
        else:
            tolerance = f"rel={self._rel!r}, abs={self._abs!r}"
        return f"approx({self._expected!r} {tolerance})"


def approx(
    expected: complex,
    *,
    rel: float | None = None,
    abs: float | None = None,
    nan_ok: bool = False,
) -> Approx:
    """`assert value == velox.approx(0.3)`.

    Scalars only: `int`, `float`, and `complex`.
    """
    return Approx(expected, rel=rel, abs=abs, nan_ok=nan_ok)
