"""Fixtures one directory down, including the autouse fixture covering only that directory.

Contributes the directory-scoped declaration case (`api_seed`, which becomes a `velox.use(...)` in
an `api/__init__.py`) and the import a declaration needs across directories, since the fixture it
depends on is written in the directory above.
"""

import pytest


@pytest.fixture(autouse=True)
def api_seed(ledger):
    ledger.append("api")


@pytest.fixture
def client(ledger):
    return {"ledger": ledger}
