"""Root fixtures: the base of every chain a subdirectory overrides.

Contributes the chain-copy case (`settings`, overridden under `integration/`, with `engine` and
`client` written here and reaching it), the leaf-override case (`token`, overridden under `leaf/`
with nothing downstream of it), the helper-a-copy-carries case (`stamp`, read by `client`'s body
rather than requested), and an autouse fixture no override touches (`ledger`).
"""

import json

import pytest

from support import stamp

LABEL = "showcase"


@pytest.fixture(scope="session")
def settings():
    return {"dsn": "sqlite://root"}


@pytest.fixture
def engine(settings):
    return f"engine:{settings['dsn']}"


@pytest.fixture
def client(engine):
    return json.loads(json.dumps({"engine": engine, "stamp": stamp(LABEL)}))


@pytest.fixture
def token():
    return "token:root"


@pytest.fixture(autouse=True)
def ledger():
    yield
