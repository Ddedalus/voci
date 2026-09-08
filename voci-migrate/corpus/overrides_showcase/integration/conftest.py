"""The override this suite exists for: `settings` redefined, with two fixtures downstream of it.

`settings` requests the definition it overrides, so its chain is the root one followed by this;
`engine` and `client` are written in the root conftest and reach this definition from here, which
is what a specialized copy of each is for. `report` is written beside the override and requests a
fixture that is copied, so it names the copy rather than the original.
"""

import pytest


@pytest.fixture
def settings(settings):
    return {**settings, "dsn": "sqlite://integration"}


@pytest.fixture
def report(client):
    return f"report:{client['engine']}"
