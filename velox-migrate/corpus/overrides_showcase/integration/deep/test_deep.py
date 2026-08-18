"""A test a directory below the override, which resolves the same specialized chain."""


def test_deep_engine(engine):
    assert engine == "engine:sqlite://integration"
