"""Root fixtures for the suite the two call-site parametrizations are measured against.

Contributes the indirect-parametrization target (`backend`), a fixture that reaches it without
naming a case of its own (`engine`), and the conftest half of the `pytest_generate_tests` case:
a hook that gives every test asking for `letter` its cases.
"""

import pytest


@pytest.fixture
def backend(request):
    return {"name": request.param, "dsn": f"{request.param}://parametrize"}


@pytest.fixture
def engine(backend):
    return {"dsn": backend["dsn"], "open": True}


def pytest_generate_tests(metafunc):
    if "letter" in metafunc.fixturenames:
        metafunc.parametrize("letter", ["a", "b"])
