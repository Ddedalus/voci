"""The over-budget override: `settings` is redefined here, and six fixtures sit downstream of it.

Every one of `layer_a` through `layer_f` is defined in the root conftest and none is redefined, so
under this directory each of them reaches this `settings` instead of the root one. `patched_env`
is redefined too, which is the case where two directories declare an autouse fixture of the same
name and only one of them patches anything.
"""

import pytest


@pytest.fixture
def settings():
    return {"dsn": "postgres://deep"}


@pytest.fixture(autouse=True)
def patched_env():
    yield
