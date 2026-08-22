"""Fixtures shared across the `test_convert*.py` modules. Plain factories live in `_support.py`."""

from __future__ import annotations

import pytest
from _support import PYTEST_VERSIONS


@pytest.fixture(params=PYTEST_VERSIONS, ids=[f"pytest{v}" for v in PYTEST_VERSIONS])
def version(request: pytest.FixtureRequest) -> str:
    return str(request.param)
