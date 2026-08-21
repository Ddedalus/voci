"""Indirect parametrization: the call site deciding which case a fixture builds.

Contributes the three shapes the values have to travel through — a target in a `conftest.py`
requested by name, the same target reached through a second fixture that names no case of its
own, and a target written in the test module beside the tests that parametrize it.
"""

import pytest


@pytest.fixture
def flavour(request):
    return request.param.upper()


@pytest.mark.parametrize("backend", ["mysql", "sqlite"], indirect=True)
def test_backend_directly(backend):
    assert backend["dsn"].endswith("://parametrize")


@pytest.mark.parametrize("backend", ["mysql", "sqlite"], indirect=True)
def test_backend_through_engine(engine):
    assert engine["open"]
    assert engine["dsn"].split("://")[0] in ("mysql", "sqlite")


@pytest.mark.parametrize("flavour", ["salty", "sweet"], indirect=True)
def test_flavour(flavour):
    assert flavour in ("SALTY", "SWEET")
