"""Root fixtures: every fixture kind, every scope, and the non-fixture content that travels
with them.

Contributes the plain-`def`, `yield`-teardown, `async def` and async-generator fixture cases, the
two-levels-deep dependency chain (`connection` on `engine` on `dsn`), the session, module and
function scopes plus the two with no counterpart (`class_scoped`, `package_scoped`), both
`params=` shapes (`backend`, which spells its own ids, and `retries`, which does not), the
renamed fixture (`probe`), and the helper function and module constant a fixture body reads
(`_labelled`, `DEFAULT_DSN`).
"""

import pytest

DEFAULT_DSN = "sqlite:///showcase"


def _labelled(prefix, value):
    """A plain helper, not a fixture — it has to travel with the fixtures that call it."""
    return f"{prefix}({value})"


@pytest.fixture(scope="session")
def dsn():
    return DEFAULT_DSN


@pytest.fixture(scope="module")
def engine(dsn):
    return _labelled("engine", dsn)


@pytest.fixture
def connection(engine):
    return _labelled("connection", engine)


@pytest.fixture
def ledger():
    entries = []
    yield entries
    entries.clear()


@pytest.fixture
async def clock():
    return 1000.0


@pytest.fixture
async def channel():
    messages = []
    yield messages
    messages.clear()


@pytest.fixture(params=["sqlite", "postgres"], ids=["lite", "pg"])
def backend(request):
    return request.param


@pytest.fixture(params=[1, 2])
def retries(request):
    return request.param


@pytest.fixture(name="probe")
def _probe():
    return _labelled("probe", DEFAULT_DSN)


@pytest.fixture(scope="class")
def class_scoped():
    return {"shared": "class"}


@pytest.fixture(scope="package")
def package_scoped():
    return {"shared": "package"}
