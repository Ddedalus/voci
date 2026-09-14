"""`voci._affected.environ`: the `os.environ` recorder behind M4's `env:` dependency
(`plans/affected-tests-plan.md`, Non-code dependencies design section)."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from voci._affected import environ
from voci._affected.collector import Collector, active


@pytest.fixture(autouse=True)
def _uninstall_after() -> Iterator[None]:
    yield
    environ.uninstall()


def test_reading_an_env_var_through_getitem_records_it() -> None:
    os.environ["VOCI_TEST_AUDIT_VAR"] = "1"
    try:
        environ.install()
        c = Collector()
        with active(c):
            _ = os.environ["VOCI_TEST_AUDIT_VAR"]
        assert c.env_names == {"VOCI_TEST_AUDIT_VAR"}
    finally:
        del os.environ["VOCI_TEST_AUDIT_VAR"]


def test_reading_through_get_and_getenv_and_in_and_copy_all_get_recorded() -> None:
    """The probe behind this module's own docstring: every one of these routes through
    `__getitem__` under the hood, so patching only that one method is enough."""
    os.environ["VOCI_TEST_AUDIT_VAR"] = "1"
    try:
        environ.install()
        c = Collector()
        with active(c):
            os.environ.get("VOCI_TEST_AUDIT_VAR")
            os.getenv("VOCI_TEST_AUDIT_VAR")
            assert "VOCI_TEST_AUDIT_VAR" in os.environ
            os.environ.copy()
        assert "VOCI_TEST_AUDIT_VAR" in c.env_names
    finally:
        del os.environ["VOCI_TEST_AUDIT_VAR"]


def test_reading_a_missing_env_var_still_records_it() -> None:
    assert "VOCI_TEST_MISSING_VAR" not in os.environ
    environ.install()
    c = Collector()
    with active(c), pytest.raises(KeyError):
        _ = os.environ["VOCI_TEST_MISSING_VAR"]
    assert "VOCI_TEST_MISSING_VAR" in c.env_names


def test_nothing_is_recorded_with_no_current_collector() -> None:
    os.environ["VOCI_TEST_AUDIT_VAR"] = "1"
    try:
        environ.install()
        assert os.environ["VOCI_TEST_AUDIT_VAR"] == "1"  # must not raise with no collector
    finally:
        del os.environ["VOCI_TEST_AUDIT_VAR"]


def test_nothing_is_recorded_once_uninstalled() -> None:
    os.environ["VOCI_TEST_AUDIT_VAR"] = "1"
    try:
        environ.install()
        environ.uninstall()
        c = Collector()
        with active(c):
            _ = os.environ["VOCI_TEST_AUDIT_VAR"]
        assert c.env_names == frozenset()
    finally:
        del os.environ["VOCI_TEST_AUDIT_VAR"]


def test_install_is_idempotent_only_the_first_call_reports_installing() -> None:
    assert environ.install() is True
    assert environ.install() is False
    environ.uninstall()


def test_uninstall_without_install_is_a_no_op() -> None:
    environ.uninstall()  # must not raise
    os.environ["VOCI_TEST_AUDIT_VAR"] = "1"  # os.environ is still perfectly usable afterward
    try:
        assert os.environ["VOCI_TEST_AUDIT_VAR"] == "1"
    finally:
        del os.environ["VOCI_TEST_AUDIT_VAR"]
