"""Root fixtures: the ones every test inherits without naming, and the ones a mark names for it.

Contributes the session-wide declaration case (`root_stamp`, autouse at the rootdir, which becomes
a `velox.use(...)` in a root `__init__.py` that is not there yet), the ordinary fixture an autouse
fixture depends on (`ledger`), the fixture the ini file's own `usefixtures` names (`audited`), and
the two a `usefixtures` mark names — one for a whole module, one for a single test.
"""

import pytest


@pytest.fixture
def ledger():
    return []


@pytest.fixture(autouse=True)
def root_stamp(ledger):
    ledger.append("root")
    yield
    ledger.append("teardown")


@pytest.fixture
def audited(ledger):
    ledger.append("audited")


@pytest.fixture
def counted(ledger):
    ledger.append("counted")


@pytest.fixture
def tracked(ledger):
    ledger.append("tracked")


@pytest.fixture
def grouped(ledger):
    ledger.append("grouped")
