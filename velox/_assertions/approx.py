"""`approx`: tolerant `==` comparison for a number, or a list, tuple, or dict of numbers."""

from __future__ import annotations

from math import isclose, isnan
from types import NotImplementedType
from typing import Any, final

__all__ = ["Approx", "approx"]

type ApproxExpected = complex | list[Any] | tuple[Any, ...] | dict[Any, Any]


@final
class Approx:
    """Tolerant numeric comparison. Compare with `==` in either direction.

    A list or tuple compares elementwise by position and a dict compares elementwise by key, both
    under the same `rel`/`abs`/`nan_ok` tolerances applied to every element. A list, tuple, dict,
    or set nested inside a list, tuple, or dict raises `TypeError`, since `approx` only walks one
    level, and so does a set at the top level: there is no position to compare it by.
    """

    __slots__ = ("_abs", "_expected", "_nan_ok", "_rel")

    DEFAULT_REL = 1e-6
    DEFAULT_ABS = 1e-12

    def __init__(
        self, expected: ApproxExpected, *, rel: float | None, abs: float | None, nan_ok: bool
    ) -> None:
        _reject_unsupported(expected)
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
        expected = self._expected
        if isinstance(expected, dict):
            return self._eq_dict(expected, actual)
        if isinstance(expected, list | tuple):
            return self._eq_sequence(expected, actual)
        return self._eq_scalar(expected, actual)

    def _eq_scalar(self, expected: complex, actual: object) -> bool | NotImplementedType:
        if not isinstance(actual, int | float | complex) or isinstance(actual, bool):
            return NotImplemented
        rel_tol, abs_tol = self._tolerances()
        if isinstance(expected, complex) or isinstance(actual, complex):
            expected_c = complex(expected)
            actual_c = complex(actual)
            # Same "or" as `isclose`: whichever tolerance is looser wins, scaled by the larger
            # magnitude so it stays symmetric in both comparison directions.
            tolerance = max(rel_tol * max(abs(expected_c), abs(actual_c)), abs_tol)
            return abs(actual_c - expected_c) <= tolerance
        expected_f = float(expected)
        if isnan(expected_f) or isnan(float(actual)):
            return self._nan_ok and isnan(expected_f) and isnan(float(actual))
        return isclose(float(actual), expected_f, rel_tol=rel_tol, abs_tol=abs_tol)

    def _eq_sequence(self, expected: list[Any] | tuple[Any, ...], actual: object) -> bool:
        if not isinstance(actual, list | tuple) or len(actual) != len(expected):
            return False
        return all(
            self._eq_scalar(item, value) is True
            for item, value in zip(expected, actual, strict=True)
        )

    def _eq_dict(self, expected: dict[Any, Any], actual: object) -> bool:
        if not isinstance(actual, dict) or actual.keys() != expected.keys():
            return False
        return all(self._eq_scalar(value, actual[key]) is True for key, value in expected.items())

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


def _reject_unsupported(expected: ApproxExpected) -> None:
    """Raise for a shape `approx` cannot compare: a set, or a container nested in one of its own
    kind."""
    if isinstance(expected, set | frozenset):
        raise TypeError(
            f"approx() does not support sets, because there is no position to compare by: "
            f"{expected!r}"
        )
    if isinstance(expected, dict):
        for key, value in expected.items():
            if isinstance(value, list | tuple | dict | set | frozenset):
                raise TypeError(
                    f"approx() does not support a {type(value).__name__} nested inside a dict, "
                    f"because it only walks one level: {key!r}: {value!r}"
                )
    elif isinstance(expected, list | tuple):
        for index, value in enumerate(expected):
            if isinstance(value, list | tuple | dict | set | frozenset):
                raise TypeError(
                    f"approx() does not support a {type(value).__name__} nested inside a "
                    f"{type(expected).__name__}, because it only walks one level: "
                    f"{value!r} at index {index}"
                )


def approx(
    expected: ApproxExpected,
    *,
    rel: float | None = None,
    abs: float | None = None,
    nan_ok: bool = False,
) -> Approx:
    """`assert value == velox.approx(0.3)`.

    `expected` is a number, or a list, tuple, or dict of numbers compared elementwise under the
    same tolerances. A set raises `TypeError`, as does a container nested inside a list, tuple,
    or dict.
    """
    return Approx(expected, rel=rel, abs=abs, nan_ok=nan_ok)
