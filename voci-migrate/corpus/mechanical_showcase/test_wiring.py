"""Fixture wiring: a fixture written in the test module, the builtin fixtures, and the class
shapes.

Contributes the fixture-defined-beside-its-tests case (`local_counter`, which needs no import
after conversion), the `tmp_path` and `tmp_path_factory` cases, the chain read out of the root
conftest, both `params=` fixtures as they reach a test, `class Test*` grouping with a nested
`class Test*`, and the class-level `pytestmark`.
"""

import pytest


@pytest.fixture
def local_counter():
    return {"n": 0}


def test_module_fixture(local_counter):
    local_counter["n"] += 1
    assert local_counter["n"] == 1


def test_fixture_chain(connection, probe):
    assert connection == "connection(engine(sqlite:///showcase))"
    assert probe == "probe(sqlite:///showcase)"


def test_yield_teardown(ledger):
    ledger.append("entry")
    assert ledger == ["entry"]


def test_tmp_path(tmp_path, tmp_path_factory):
    target = tmp_path / "note.txt"
    target.write_text("written")
    shared = tmp_path_factory.mktemp("shared")

    assert target.read_text() == "written"
    assert shared.is_dir()


def test_params_fixture_with_ids(backend):
    assert backend in ("sqlite", "postgres")


def test_params_fixture_without_ids(retries):
    assert retries in (1, 2)


class TestGrouped:
    pytestmark = pytest.mark.classmark

    def test_class_scoped(self, class_scoped):
        assert class_scoped == {"shared": "class"}

    def test_module_fixture_from_a_method(self, local_counter):
        assert local_counter == {"n": 0}


class TestOuter:
    def test_outer(self, dsn):
        assert dsn == "sqlite:///showcase"

    class TestNested:
        def test_nested(self, ledger):
            assert ledger == []
