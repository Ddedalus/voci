"""One of the two sibling directories that both define a fixture named `client`.

Legal in pytest because neither directory is above the other, so nothing here overrides anything.
Contributes half of the two-siblings-one-name case, together with `beta/`, plus the only consumer
of the root conftest's `scope="package"` fixture.
"""

import pytest


@pytest.fixture
def client():
    return {"transport": "alpha"}
