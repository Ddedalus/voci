"""Root fixtures for the suite the body rewrites are measured against.

Contributes the dynamic-request cases the scan is the only witness for: the literal
`request.getfixturevalue` a static dependency hides behind (`engine`), the unconditional
`request.addfinalizer` that a `yield` says the same way (`ledger`, `journal`), and the
session-scoped list those finalizers write into so a later test can read what teardown did
(`teardowns`).
"""

import pytest


@pytest.fixture(scope="session")
def settings():
    return {"dsn": "sqlite:///bodies"}


@pytest.fixture(scope="session")
def teardowns():
    return []


@pytest.fixture
def engine(request):
    return {"dsn": request.getfixturevalue("settings")["dsn"], "open": True}


@pytest.fixture
def ledger(request, teardowns):
    entries = []

    def close():
        teardowns.append("ledger")

    request.addfinalizer(close)
    return entries


@pytest.fixture
def journal(request, teardowns):
    request.addfinalizer(lambda: teardowns.append("journal-outer"))
    request.addfinalizer(lambda: teardowns.append("journal-inner"))
    return {"open": True}
