"""`approx`: tolerant `==` comparison for a number or a collection of numbers."""

from __future__ import annotations

from math import isclose, isnan
from types import NotImplementedType
from typing import final

__all__ = ["Approx", "approx"]


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
