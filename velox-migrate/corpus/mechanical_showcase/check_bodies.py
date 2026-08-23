"""Test bodies whose pytest API has a velox counterpart, plus the skipif written as a string.

Contributes `pytest.raises` in both its bare and `match=` forms, `pytest.approx` over a scalar,
`capsys` read exactly once, `caplog` read through `.records` and `.messages`, `pytest.skip`/
`pytest.fail` called as a statement (never actually reached, so the suite stays green either way
its module runs), and the string `skipif` condition — whose module *does* import `sys`, since the
imperative forms need it for their own always-false guard, unlike the string condition, which
pytest evaluates with `sys` already in scope regardless of what the module itself imports.
Named `check_*.py` rather than `test_*.py`, which is the second pattern this suite's
`python_files` setting spells.
"""

import logging
import sys

import pytest


def test_raises():
    with pytest.raises(KeyError):
        {"present": 1}["missing"]

    with pytest.raises(ValueError, match="not a number"):
        int("not a number")


def test_approx():
    assert 0.1 + 0.2 == pytest.approx(0.3)


def test_capsys(capsys):
    print("one line of output")

    assert capsys.readouterr().out == "one line of output\n"


def test_caplog(caplog):
    logging.getLogger("showcase").warning("disk almost full")

    assert [record.levelname for record in caplog.records] == ["WARNING"]
    assert list(caplog.messages) == ["disk almost full"]


@pytest.mark.skipif("sys.platform == 'nonexistent'", reason="written as a string expression")
def test_skipif_string_condition():
    assert True


def test_imperative_skip():
    if sys.platform == "nonexistent":
        pytest.skip("no backend on this platform")
    assert True


def test_imperative_fail():
    if sys.platform == "nonexistent":
        pytest.fail("unreachable on this platform")
    assert True
