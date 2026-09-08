"""Tests that resolve the whole layer chain through the overridden `settings`."""


def test_deep_chain(layer_f):
    assert layer_f["dsn"] == "postgres://deep"


def test_shallow_chain(layer_a):
    assert layer_a["layer"] == "a"
