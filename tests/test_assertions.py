"""Tests for velox's `raises`/`approx` assertion helpers."""

from __future__ import annotations

import asyncio

import pytest

import velox


def test_approx_is_unhashable() -> None:
    """A tolerant `__eq__` cannot have a consistent hash — unhashable, like pytest's
    `ApproxBase`."""
    with pytest.raises(TypeError):
        hash(velox.approx(1.0))
    with pytest.raises(TypeError):
        _ = {velox.approx(1.0): "x"}


def test_approx_abs_zero_is_exact_for_complex() -> None:
    """An explicit `abs=0` must not be treated as "unset" in the complex branch."""
    assert complex(1.0) == velox.approx(complex(1.0), abs=0, rel=0)
    assert complex(1.0000000000001) != velox.approx(complex(1.0), abs=0, rel=0)


def test_approx_abs_alone_is_not_widened_by_the_relative_default() -> None:
    """Naming `abs` without `rel` must mean *only* the absolute tolerance, not loosened by
    the 1e-6 relative default the way `isclose`'s "loosest wins" rule would."""
    # `value == approx(expected)` is the documented reading order, hence the SIM300 waivers.
    assert 1.0000000000001 != velox.approx(1.0, abs=0)  # noqa: SIM300
    assert 1.0000001 != velox.approx(1.0, abs=1e-13)  # noqa: SIM300
    assert 1.00000000000001 == velox.approx(1.0, abs=1e-13)  # noqa: SIM300
    # Same rule on the complex branch.
    assert complex(1.0000000000001) != velox.approx(complex(1.0), abs=0)


def test_approx_abs_only_matches_pytest() -> None:
    """Matches `pytest.approx`'s tolerance rule."""
    for actual, expected, tol in [(1.0000000000001, 1.0, 0), (1.0000001, 1.0, 1e-13)]:
        assert (actual == velox.approx(expected, abs=tol)) == (
            actual == pytest.approx(expected, abs=tol)
        )


def test_approx_rel_alone_keeps_the_absolute_floor() -> None:
    """Naming `rel` alone keeps `DEFAULT_ABS` as the floor underneath it."""
    assert 0.0 == velox.approx(0.0, rel=1e-6)  # noqa: SIM300
    assert 1e-13 == velox.approx(0.0, rel=1e-6)  # noqa: SIM300


def test_approx_honors_rel_for_complex() -> None:
    """`rel=` must be honored for complex operands, not just real ones."""
    assert complex(1000, 0) == velox.approx(complex(1000.0001, 0), rel=1e-3)
    assert complex(1000, 0) != velox.approx(complex(1000.0001, 0), rel=1e-12)


def test_approx_repr_reflects_an_explicit_abs_zero() -> None:
    assert repr(velox.approx(1.0, abs=0)) == "approx(1.0 rel=None, abs=0)"


def test_approx_repr_is_built_from_the_default_constants() -> None:
    assert repr(velox.approx(0.3)) == f"approx(0.3 ±{velox.Approx.DEFAULT_REL!r})"


def test_exception_info_value_raises_loudly_before_the_block_completes() -> None:
    """Reading `.value` before the block completes raises `RuntimeError`, not the `AttributeError`
    that `hasattr`/`getattr` would swallow."""
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
    with pytest.raises(TypeError, match="CancelledError"):
        velox.raises(asyncio.CancelledError)
    with pytest.raises(TypeError, match="CancelledError"):
        velox.raises((ValueError, asyncio.CancelledError))


def test_raises_still_catches_system_exit_and_keyboard_interrupt() -> None:
    """Only `CancelledError` is rejected; `SystemExit`/`KeyboardInterrupt` are ordinary
    exception types as far as `raises()` is concerned."""
    with velox.raises(SystemExit):
        raise SystemExit(1)
    with velox.raises(KeyboardInterrupt):
        raise KeyboardInterrupt
