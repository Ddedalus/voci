"""Regression tests for velox._assertions (spec/07's `raises`/`approx`)."""

from __future__ import annotations

import asyncio

import pytest
import velox


def test_approx_is_unhashable() -> None:
    """A tolerant `__eq__` cannot have a consistent hash — unhashable, like pytest's ApproxBase,
    rather than a container that quietly loses the key."""
    with pytest.raises(TypeError):
        hash(velox.approx(1.0))
    with pytest.raises(TypeError):
        _ = {velox.approx(1.0): "x"}


def test_approx_abs_zero_is_exact_for_complex() -> None:
    """An explicit `abs=0` must not be treated as "unset" in the complex branch."""
    assert complex(1.0) == velox.approx(complex(1.0), abs=0, rel=0)
    assert complex(1.0000000000001) != velox.approx(complex(1.0), abs=0, rel=0)


def test_approx_abs_alone_is_not_widened_by_the_relative_default() -> None:
    """Naming `abs` without `rel` must mean *only* the absolute tolerance.

    Combining the two the way `isclose` does — loosest wins — let the 1e-6 relative default
    swallow the tolerance the caller asked for, so `abs=` behaved as a floor rather than a
    ceiling. Passing `rel=0` alongside `abs=` hides this, so these cases deliberately do not.
    """
    # `value == approx(expected)` is the documented reading order, hence the SIM300 waivers.
    assert 1.0000000000001 != velox.approx(1.0, abs=0)  # noqa: SIM300
    assert 1.0000001 != velox.approx(1.0, abs=1e-13)  # noqa: SIM300
    assert 1.00000000000001 == velox.approx(1.0, abs=1e-13)  # noqa: SIM300
    # Same rule on the complex branch.
    assert complex(1.0000000000001) != velox.approx(complex(1.0), abs=0)


def test_approx_abs_only_matches_pytest() -> None:
    """Parity with `pytest.approx`, whose documented rule this mirrors."""
    for actual, expected, tol in [(1.0000000000001, 1.0, 0), (1.0000001, 1.0, 1e-13)]:
        assert (actual == velox.approx(expected, abs=tol)) == (
            actual == pytest.approx(expected, abs=tol)
        )


def test_approx_rel_alone_keeps_the_absolute_floor() -> None:
    """The converse: naming `rel` alone keeps DEFAULT_ABS underneath, which is what makes a
    comparison against zero work at all."""
    assert 0.0 == velox.approx(0.0, rel=1e-6)  # noqa: SIM300
    assert 1e-13 == velox.approx(0.0, rel=1e-6)  # noqa: SIM300


def test_approx_honors_rel_for_complex() -> None:
    """`rel=` used to be silently dropped for complex operands."""
    assert complex(1000, 0) == velox.approx(complex(1000.0001, 0), rel=1e-3)
    assert complex(1000, 0) != velox.approx(complex(1000.0001, 0), rel=1e-12)


def test_approx_repr_reflects_an_explicit_abs_zero() -> None:
    assert repr(velox.approx(1.0, abs=0)) == "approx(1.0 rel=None, abs=0)"


def test_approx_repr_is_built_from_the_default_constants() -> None:
    assert repr(velox.approx(0.3)) == f"approx(0.3 ±{velox.Approx.DEFAULT_REL!r})"


def test_exception_info_value_raises_loudly_before_the_block_completes() -> None:
    """AttributeError from a property is swallowed by hasattr/getattr's default; this must not
    be one, so that reading `.value` too early cannot be mistaken for "no exception"."""
    with velox.raises(ValueError) as caught:
        with pytest.raises(RuntimeError, match="has not completed"):
            _ = caught.value
        with pytest.raises(RuntimeError):
            getattr(caught, "value", "sentinel")
        with pytest.raises(RuntimeError):
            hasattr(caught, "value")
        raise ValueError("boom")

    assert caught.value.args == ("boom",)


def test_raises_rejects_asyncio_cancelled_error() -> None:
    """velox's own timeout machinery relies on CancelledError to stop a runaway test; a
    raises() block that could swallow it would make that test un-timeout-able."""
    with pytest.raises(TypeError, match="CancelledError"):
        velox.raises(asyncio.CancelledError)
    with pytest.raises(TypeError, match="CancelledError"):
        velox.raises((ValueError, asyncio.CancelledError))


def test_raises_still_catches_system_exit_and_keyboard_interrupt() -> None:
    """Testing a CLI's SystemExit is legitimate and common; only CancelledError is guarded."""
    with velox.raises(SystemExit):
        raise SystemExit(1)
    with velox.raises(KeyboardInterrupt):
        raise KeyboardInterrupt
