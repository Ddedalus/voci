"""Root-level fixtures: plain fixtures, autouse, fixture params=, and two wrapped fixtures.

Contributes the base of the override-chain case (`settings`, overridden in `integration/`), the
autouse-placement case (`root_autouse`), the fixture-params-become-callspec case (`backend`), the
dynamic-fixture-use-is-invisible case (`dyn`, via `request.getfixturevalue`), the
see-through-the-wrapper case (`wrapped_fix`), and the wrapper-that-cannot-be-seen-through case
(`opaque_fix`, whose factory lives in `helpers.py` while the fixture belongs to this conftest).
"""

import functools

import pytest

from helpers import opaque


@pytest.fixture(scope="session")
def settings():
    return {"dsn": "sqlite://"}


@pytest.fixture
def engine(settings):
    return f"engine:{settings['dsn']}"


@pytest.fixture(autouse=True)
def root_autouse():
    yield


@pytest.fixture(params=["sqlite", "postgres"], ids=["lite", "pg"])
def backend(request):
    return request.param


@pytest.fixture
def hidden():
    return "hidden-value"


@pytest.fixture
def dyn(request):
    return request.getfixturevalue("hidden")


def _decorate(fn):
    @functools.wraps(fn)
    def inner(*args, **kwargs):
        return fn(*args, **kwargs)

    return inner


@pytest.fixture
@_decorate
def wrapped_fix():
    return "wrapped-value"


@pytest.fixture
@opaque
def opaque_fix():
    return "opaque-value"
