"""Test bodies holding the pytest API whose voci counterpart is partial or absent.

Contributes the capture-snapshot case (`readouterr` twice), the caplog cases (`set_level` and
`.handler`), and the no-counterpart cases (`importorskip`, `pytest.xfail()` as a statement --
unlike `pytest.skip()`/`pytest.fail()`, which become `raise voci.Skipped(...)`/`Failed(...)`,
there is no runtime target an expectation `@voci.xfail(...)` decides once, at collection, could
become -- `warns`, a `raises` object stashed rather than entered or called, `approx` over a
generator, `capfd`, `recwarn`, `tmpdir`), plus both `mock.patch` forms and every `request` escape
hatch the root conftest wires up.
"""

import logging
import os
import sys
import warnings
from unittest import mock

import pytest


def test_double_readouterr(capsys):
    print("one")
    first = capsys.readouterr()
    print("two")
    second = capsys.readouterr()
    assert first.out != second.out


def test_caplog_level(caplog):
    caplog.set_level(logging.INFO)
    logging.getLogger("app").info("hello")
    caplog.handler.flush()


def test_conditional_xfail():
    if sys.platform == "nonexistent":
        pytest.xfail("this platform has no such thing")
    assert True


def test_importorskip():
    module = pytest.importorskip("json")
    assert module


def test_warns():
    with pytest.warns(UserWarning):
        warnings.warn("careful", UserWarning, stacklevel=1)


def test_legacy_raises():
    stashed = pytest.raises(ValueError)


def test_approx_generator():
    stashed = pytest.approx(x for x in [0.3])


def test_legacy_tmpdir(legacy_dir, tmpdir):
    tmpdir.join("other.txt").write("x")
    assert legacy_dir.strpath


def test_capfd(capfd):
    print("through the file descriptor")
    assert capfd.readouterr().out


def test_recwarn(recwarn):
    warnings.warn("noted", stacklevel=1)
    assert len(recwarn) == 1


@mock.patch("os.getcwd")
def test_patch_decorator(getcwd, node_name):
    getcwd.return_value = "/nowhere"
    assert node_name


def test_patch_context_manager():
    with mock.patch.dict(os.environ, {"MODE": "off"}):
        assert os.environ["MODE"] == "off"


def test_request_escape_hatches(node_name, option, closed, sometimes_closed, computed, literal):
    assert node_name and option and closed and sometimes_closed and computed and literal
