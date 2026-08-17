"""Subdirectory fixtures, none of which shares a name with anything above them.

Contributes the second fixture module the layout planner has to place, the cross-directory import
(this directory's tests read the root conftest's fixtures in the same signature), and the name
whose import has to be aliased — `test_api.py` binds `payload` itself.
"""

import pytest


@pytest.fixture
def payload():
    return {"kind": "order", "qty": 2}


@pytest.fixture
def route(payload):
    return f"/v1/{payload['kind']}s"
