"""Root-level tests: direct parametrize, indirect parametrize, fixture-params-only parametrize,
usefixtures plus a lazily-evaluated skipif, dynamic fixture use, and module/class pytestmark.
"""

import pytest

pytestmark = pytest.mark.modmark


@pytest.mark.parametrize(
    "n",
    [1, pytest.param(2, id="two", marks=pytest.mark.xfail(reason="boom")), 3.5],
)
def test_direct(n):
    assert n in (1, 2, 3.5)


@pytest.mark.parametrize("backend", ["mysql"], indirect=True)
def test_indirect(backend):
    assert backend == "mysql"


def test_params_fixture(backend):
    assert backend in ("sqlite", "postgres")


@pytest.mark.usefixtures("wrapped_fix")
@pytest.mark.skipif(condition="sys.platform == 'nonexistent'", reason="never")
def test_uses(dyn):
    assert dyn == "hidden-value"


class TestGroup:
    pytestmark = pytest.mark.classmark

    def test_method(self, settings):
        assert settings["dsn"] == "sqlite://"


def test_requests_an_autouse_fixture_explicitly(root_autouse):
    assert root_autouse is None
