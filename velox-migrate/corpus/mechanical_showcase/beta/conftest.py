"""The other sibling directory defining a fixture named `client`. See `alpha/conftest.py`."""

import pytest


@pytest.fixture
def client():
    return {"transport": "beta"}
