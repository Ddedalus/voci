"""Subdirectory tests: prove a root fixture picks up an overridden dependency transparently.

`engine` is defined once, in the root conftest, and depends on `settings` — but under this
directory `settings` resolves to the `integration/conftest.py` override, not the root definition.
"""


def test_engine(engine):
    assert engine == "engine:sqlite://integration"


def test_settings(settings):
    assert settings["dsn"] == "sqlite://integration"
