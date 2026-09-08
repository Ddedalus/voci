"""Marks: every parametrize shape and every skip/xfail/tag/timeout shape that converts.

Contributes the two-argname form over `str`/`int`/`bool`/`None` values, the `float` and `tuple`
values whose generated ids pytest and voci spell differently, `pytest.param(v, id=...)`, two
stacked marks, two single-case axes whose composed id can be attributed to neither of them, both
`skip` spellings, a boolean `skipif`, all three `xfail` shapes, a custom mark used as a tag,
`@pytest.mark.timeout`, and the module-level `pytestmark`.
"""

import sys

import pytest

pytestmark = pytest.mark.modmark


@pytest.mark.parametrize("label,count", [("text", 7), (None, True)])
def test_parametrize_two_argnames(label, count):
    assert count
    assert label is None or label == "text"


@pytest.mark.parametrize("value", [0.25, (1, 2), pytest.param("named", id="explicit")])
def test_parametrize_generated_ids(value):
    assert value


@pytest.mark.parametrize("outer", ["o1", "o2"])
@pytest.mark.parametrize("inner", ["i1", "i2"])
def test_parametrize_stacked(outer, inner):
    assert outer.startswith("o")
    assert inner.startswith("i")


@pytest.mark.parametrize("region", ["eu"])
@pytest.mark.parametrize("shard", ["primary"])
def test_parametrize_single_case_axes(region, shard):
    assert region == "eu"
    assert shard == "primary"


@pytest.mark.skip
def test_skip_without_a_reason():
    raise AssertionError("a skipped test never runs its body")


@pytest.mark.skip(reason="the behaviour it covers is not written yet")
def test_skip_with_a_reason():
    raise AssertionError("a skipped test never runs its body")


@pytest.mark.skipif(sys.version_info < (3, 13), reason="the body reads a 3.13 interpreter")
def test_skipif_boolean():
    assert sys.version_info >= (3, 13)


@pytest.mark.xfail(reason="the parser accepts a trailing comma it should reject")
def test_xfail():
    raise AssertionError("trailing comma accepted")


@pytest.mark.xfail(reason="fails on a filesystem without atomic rename", strict=False)
def test_xfail_not_strict():
    raise AssertionError("rename was not atomic")


@pytest.mark.xfail(reason="the loader rejects an empty document", raises=ValueError)
def test_xfail_raises():
    raise ValueError("empty document")


@pytest.mark.slow
@pytest.mark.timeout(30)
def test_tagged_and_timed():
    assert True
