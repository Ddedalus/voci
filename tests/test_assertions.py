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


def test_approx_compares_a_list_elementwise() -> None:
    assert [0.1 + 0.2, 0.2 + 0.4] == velox.approx([0.3, 0.6])  # noqa: SIM300
    assert (0.1 + 0.2, 0.2 + 0.4) == velox.approx((0.3, 0.6))  # noqa: SIM300


def test_approx_over_a_list_fails_on_mismatched_length() -> None:
    assert velox.approx([1.0, 2.0]) != [1.0, 2.0, 3.0]
    assert velox.approx([1.0, 2.0]) != [1.0]


def test_approx_over_a_list_fails_on_a_value_outside_tolerance() -> None:
    assert velox.approx([1.0, 2.0]) != [1.0, 2.1]


def test_approx_compares_a_dict_elementwise() -> None:
    assert {"a": 0.1 + 0.2, "b": 0.2 + 0.4} == velox.approx({"a": 0.3, "b": 0.6})  # noqa: SIM300


def test_approx_over_a_dict_fails_on_mismatched_keys() -> None:
    assert velox.approx({"a": 0.3, "b": 0.6}) != {"a": 0.3, "c": 0.6}
    assert velox.approx({"a": 0.3, "b": 0.6}) != {"a": 0.3}


def test_approx_over_a_nested_list_raises() -> None:
    with pytest.raises(TypeError, match="nested"):
        velox.approx([1.0, [2.0, 3.0]])


def test_approx_over_a_nested_dict_raises() -> None:
    with pytest.raises(TypeError, match="nested"):
        velox.approx({"a": 1.0, "b": {"c": 2.0}})


def test_approx_over_a_set_raises() -> None:
    """Sets are unordered, so there is no position to compare by."""
    with pytest.raises(TypeError):
        velox.approx({1.0, 2.0})  # type: ignore[arg-type]


def test_approx_over_a_list_nested_in_a_dict_raises() -> None:
    with pytest.raises(TypeError, match="nested"):
        velox.approx({"a": [1.0, 2.0]})


def test_approx_over_a_tuple_nested_in_a_list_raises() -> None:
    with pytest.raises(TypeError, match="nested"):
        velox.approx([1.0, (2.0, 3.0)])


def test_approx_over_a_dict_nested_in_a_list_raises() -> None:
    with pytest.raises(TypeError, match="nested"):
        velox.approx([1.0, {"a": 2.0}])


def test_approx_over_a_collection_honors_rel_abs_and_nan_ok_per_element() -> None:
    assert [1.0, float("nan")] == velox.approx([1.0, float("nan")], nan_ok=True)
    assert velox.approx([1.0, float("nan")]) != [1.0, float("nan")]
    assert [1000.0] == velox.approx([1000.0001], rel=1e-3)  # noqa: SIM300
    assert velox.approx([1000.0001], rel=1e-12) != [1000.0]
    assert {"a": 1.0000000000001} == velox.approx({"a": 1.0}, abs=1e-13)  # noqa: SIM300
    assert velox.approx({"a": 1.0}, abs=1e-13) != {"a": 1.0000001}


def test_approx_over_a_collection_is_symmetric() -> None:
    assert [0.3] == velox.approx([0.1 + 0.2])  # noqa: SIM300
    assert velox.approx([0.1 + 0.2]) == [0.3]
    assert {"a": 0.3} == velox.approx({"a": 0.1 + 0.2})  # noqa: SIM300
    assert velox.approx({"a": 0.1 + 0.2}) == {"a": 0.3}


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


def test_raises_callable_form_returns_a_populated_exception_info() -> None:
    def boom() -> None:
        raise ValueError("kaboom")

    info = velox.raises(ValueError, boom)
    assert info.value.args == ("kaboom",)
    assert info.type is ValueError


def test_raises_callable_form_forwards_args_and_kwargs() -> None:
    def f(a: int, b: int, *, c: int = 0) -> None:
        raise ValueError(f"{a} {b} {c}")

    info = velox.raises(ValueError, f, 1, 2, c=3)
    assert info.value.args == ("1 2 3",)


def test_raises_callable_form_propagates_when_the_call_does_not_raise() -> None:
    def f() -> None:
        pass

    with pytest.raises(AssertionError, match="DID NOT RAISE"):
        velox.raises(ValueError, f)


def test_raises_callable_form_propagates_an_unexpected_exception_type() -> None:
    def f() -> None:
        raise TypeError("wrong kind")

    with pytest.raises(TypeError, match="wrong kind"):
        velox.raises(ValueError, f)


def test_raises_callable_form_rejects_a_non_callable_func() -> None:
    with pytest.raises(TypeError, match="not callable"):
        velox.raises(ValueError, "not a function")  # type: ignore[call-overload]


def test_raises_callable_form_rejects_asyncio_cancelled_error() -> None:
    def f() -> None:
        raise asyncio.CancelledError

    with pytest.raises(TypeError, match="CancelledError"):
        velox.raises(asyncio.CancelledError, f)


def test_raises_callable_form_match_checks_the_exception_not_the_call() -> None:
    """`match` always matches against the raised exception's message in the callable form too --
    it is never forwarded to `func` as a `**kwargs` entry the way pytest's callable form does."""

    def f(**kwargs: object) -> None:
        assert kwargs == {}, "match should not have been forwarded to func"
        raise ValueError("must be 0 or None")

    info = velox.raises(ValueError, f, match="must be 0 or None")
    assert info.value.args == ("must be 0 or None",)

    def g() -> None:
        raise ValueError("something else")

    with pytest.raises(AssertionError, match="does not match"):
        velox.raises(ValueError, g, match="must be 0 or None")


def test_raises_bare_keyword_args_without_func_are_rejected() -> None:
    """A stray keyword argument with no `func` to forward it to is almost certainly a mistake,
    not a request for the context-manager form."""
    with pytest.raises(TypeError, match="without a callable"):
        velox.raises(ValueError, unexpected=1)  # type: ignore[call-overload]
