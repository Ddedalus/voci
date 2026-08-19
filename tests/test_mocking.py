"""Tests for velox._mocking: finding `unittest.mock` patching on a test, and the guard that
refuses a patch entered by a test running alongside others."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest

from velox import _mocking
from velox._builtins import capture as _capture


@pytest.fixture
def guard() -> Iterator[None]:
    """The patch guard installed for one test, and always removed again -- it replaces a method
    on `unittest.mock`'s own classes, which outlives the test that installed it."""
    _mocking.install()
    try:
        yield
    finally:
        _mocking.uninstall()


@pytest.fixture
def running_test() -> Iterator[None]:
    """A test context that is *not* allowed to patch, the way an ordinary concurrently
    dispatched test's is."""
    token = _capture.current_test_context.set(
        _capture.TestContext(
            sink=_capture.Sink(label="t"), tags=(), timeout=None, worker=0, patching_allowed=False
        )
    )
    try:
        yield
    finally:
        _capture.current_test_context.reset(token)


@pytest.fixture
def running_solo_test() -> Iterator[None]:
    token = _capture.current_test_context.set(
        _capture.TestContext(
            sink=_capture.Sink(label="t"), tags=(), timeout=None, worker=0, patching_allowed=True
        )
    )
    try:
        yield
    finally:
        _capture.current_test_context.reset(token)


def test_an_undecorated_function_patches_nothing() -> None:
    def test_plain() -> None:
        pass

    assert _mocking.patching_of(test_plain) == _mocking.NO_PATCHING


def test_a_patch_decorator_is_found_with_the_argument_it_fills() -> None:
    @mock.patch("os.getcwd", return_value="/x")
    def test_patched(getcwd: Any) -> None:
        pass

    patching = _mocking.patching_of(test_patched)

    assert patching.targets == ("getcwd",)
    assert patching.positional_args == 1


def test_stacked_patch_decorators_are_all_found() -> None:
    @mock.patch("os.getcwd", return_value="/x")
    @mock.patch("os.getpid", return_value=1)
    def test_patched(getpid: Any, getcwd: Any) -> None:
        pass

    patching = _mocking.patching_of(test_patched)

    # Bottom decorator first, which is also the order its mock arrives in.
    assert patching.targets == ("getpid", "getcwd")
    assert patching.positional_args == 2


def test_a_patch_with_an_explicit_replacement_fills_no_argument() -> None:
    """`new=` hands the replacement in directly, so `unittest.mock` passes the test nothing."""

    @mock.patch("os.getcwd", new=lambda: "/x")
    def test_patched() -> None:
        pass

    patching = _mocking.patching_of(test_patched)

    assert patching.targets == ("getcwd",)
    assert patching.positional_args == 0


def test_patch_dict_is_found_even_though_it_records_no_patchings() -> None:
    """`mock.patch.dict`'s decorator is a closure over its patcher rather than a wrapper
    carrying a `patchings` list, so it is found through the closure."""

    @mock.patch.dict(os.environ, {"VELOX_TEST": "1"})
    def test_patched() -> None:
        pass

    patching = _mocking.patching_of(test_patched)

    assert patching.targets == ("os._Environ",)
    assert patching.positional_args == 0


def test_patch_multiple_fills_its_parameters_by_name() -> None:
    """`mock.patch.multiple` records one patcher holding the rest of its group, and passes every
    mock in by keyword rather than positionally."""

    @mock.patch.multiple("os.path", exists=mock.DEFAULT, isdir=mock.DEFAULT)
    def test_patched(exists: Any, isdir: Any) -> None:
        pass

    patching = _mocking.patching_of(test_patched)

    assert patching.targets == ("exists", "isdir")
    assert patching.positional_args == 0
    assert patching.keyword_args == frozenset({"exists", "isdir"})


def test_real_function_reads_through_decorators() -> None:
    def test_underneath(value: int = 1) -> None:
        pass

    decorated = mock.patch("os.getcwd")(test_underneath)

    assert decorated is not test_underneath
    assert _mocking.real_function(decorated) is test_underneath
    assert _mocking.real_function(test_underneath) is test_underneath


def test_real_function_stops_at_the_innermost_actual_function() -> None:
    """`__wrapped__` is an ordinary attribute anyone can set to anything; whatever velox hands
    back has to be a function, since the caller reads `__code__` off it."""

    def test_underneath() -> None:
        pass

    test_underneath.__wrapped__ = mock.MagicMock()  # type: ignore[attr-defined]

    assert _mocking.real_function(test_underneath) is test_underneath


def test_a_patch_entered_by_a_concurrently_running_test_is_refused(
    guard: None, running_test: None
) -> None:
    with (
        pytest.raises(_mocking.GlobalPatchError) as excinfo,
        mock.patch("os.getcwd", return_value="/x"),
    ):
        pass

    assert "getcwd" in str(excinfo.value)
    assert "@velox.solo" in str(excinfo.value)
    assert os.getcwd() != "/x"


def test_patch_dict_entered_by_a_concurrently_running_test_is_refused(
    guard: None, running_test: None
) -> None:
    with (
        pytest.raises(_mocking.GlobalPatchError),
        mock.patch.dict(os.environ, {"VELOX_TEST": "1"}),
    ):
        pass

    assert "VELOX_TEST" not in os.environ


def test_a_test_running_alone_patches_freely(guard: None, running_solo_test: None) -> None:
    with mock.patch("os.getcwd", return_value="/x"):
        assert os.getcwd() == "/x"


def test_a_patch_outside_any_test_is_left_alone(guard: None) -> None:
    """Nothing is running that could see the write -- a session fixture or an import-time patch
    is not the hazard the guard exists for."""
    with mock.patch("os.getcwd", return_value="/x"):
        assert os.getcwd() == "/x"


def test_uninstall_puts_unittest_mock_back(running_test: None) -> None:
    original = mock._patch.__enter__
    _mocking.install()
    assert _mocking.installed()
    assert mock._patch.__enter__ is not original

    _mocking.uninstall()

    assert not _mocking.installed()
    assert mock._patch.__enter__ is original
    with mock.patch("os.getcwd", return_value="/x"):
        assert os.getcwd() == "/x"


def test_install_is_idempotent(running_test: None) -> None:
    """A second install must not guard the guard -- one uninstall has to restore the original."""
    original = mock._patch.__enter__
    assert _mocking.install() is True
    assert _mocking.install() is False

    _mocking.uninstall()

    assert mock._patch.__enter__ is original


def test_a_nested_run_does_not_disarm_the_guard_around_it(running_test: None) -> None:
    """`install` reports who installed it precisely so a nested `run_suite` -- an embedding
    caller's, say -- leaves the enclosing run's guard alone."""
    _mocking.install()
    try:
        assert _mocking.install() is False  # what the nested run sees
        assert _mocking.installed()  # and so it never calls uninstall
        with (
            pytest.raises(_mocking.GlobalPatchError),
            mock.patch("os.getcwd", return_value="/x"),
        ):
            pass
    finally:
        _mocking.uninstall()
