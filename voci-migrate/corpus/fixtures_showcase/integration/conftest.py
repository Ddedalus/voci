"""Subdirectory fixtures: the override-chain case this corpus exists to exercise.

`settings` requests its own super fixture by the same name, producing a two-element
`name2fixturedefs` chain (root def, then this override) for anything under `integration/`.
`integ_autouse` contributes a second, narrower entry to the autouse-placement map.
"""

import pytest


@pytest.fixture
def settings(settings):
    return {**settings, "dsn": "sqlite://integration"}


@pytest.fixture(autouse=True)
def integ_autouse():
    yield
