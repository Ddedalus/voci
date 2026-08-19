"""What a test body does that only the source can show: patching, and asking for a fixture by name.

Contributes both `mock.patch` forms — the decorator, whose mock arrives in the first positional
slot ahead of every injected parameter, and the two context-manager spellings, which are invisible
until the body runs — plus a test that reaches a fixture through `request` rather than through its
own signature.
"""

import os
from unittest import mock

import pytest


@mock.patch("os.getcwd")
def test_patch_decorator(getcwd, engine):
    getcwd.return_value = "/nowhere"
    assert os.getcwd() == "/nowhere"
    assert engine["dsn"] == "sqlite:///bodies"


@pytest.mark.parametrize("attempt", [1, 2])
@mock.patch("os.getcwd")
def test_patch_decorator_parametrized(getcwd, attempt, settings):
    getcwd.return_value = f"/nowhere/{attempt}"
    assert os.getcwd().endswith(str(attempt))
    assert settings["dsn"]


def test_patch_context_manager(engine):
    with mock.patch.object(os.path, "sep", "|"):
        assert os.path.sep == "|"
    assert engine["open"]


def test_patch_dict_context_manager():
    with mock.patch.dict(os.environ, {"BODIES_MODE": "off"}):
        assert os.environ["BODIES_MODE"] == "off"


def test_started_patcher():
    patcher = mock.patch("os.getcwd")
    getcwd = patcher.start()
    getcwd.return_value = "/started"
    assert os.getcwd() == "/started"
    patcher.stop()


def test_finalizers_are_registered(ledger, journal):
    assert ledger == []
    assert journal["open"]


def test_getfixturevalue_by_name(request):
    assert request.getfixturevalue("settings")["dsn"] == "sqlite:///bodies"


def test_teardown_ran_in_reverse(teardowns):
    assert teardowns == ["journal-inner", "journal-outer", "ledger"]


def test_report_reads_the_root_list(report):
    assert report["dsn"] == "sqlite:///bodies"
