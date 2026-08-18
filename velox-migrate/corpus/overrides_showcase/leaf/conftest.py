"""The leaf override: `token` is redefined with nothing written between it and the tests."""

import pytest


@pytest.fixture
def token():
    return "token:leaf"
