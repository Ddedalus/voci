"""Marks and class shapes whose translation is not a rename.

Contributes the per-case-mark case, the string skipif condition, both xfail shapes with no
counterpart, a per-test warning filter, a scalar mark arriving twice (module-level `pytestmark`
plus a decorator), a mark a plugin would act on, the `usefixtures`-on-one-test case, indirect
parametrization of a method whose name a second class repeats, and the two class lifecycles velox
does not provide.
"""

import sys
import unittest

import pytest

pytestmark = pytest.mark.timeout(30)


@pytest.mark.parametrize(
    "value", [1, pytest.param(2, marks=pytest.mark.xfail(reason="known to fail")), 3]
)
def test_cases(value):
    assert value


@pytest.mark.skipif("sys.platform == 'nonexistent'", reason="written as a string expression")
def test_string_condition():
    assert True


@pytest.mark.xfail(sys.version_info < (3, 20), reason="only on a future Python")
def test_conditional_xfail():
    assert True


@pytest.mark.xfail(run=False, reason="known to hang")
def test_never_run():
    assert True


@pytest.mark.filterwarnings("error::UserWarning")
def test_filtered_warnings():
    assert True


@pytest.mark.timeout(5)
def test_two_timeouts():
    assert True


@pytest.mark.django_db
def test_plugin_mark():
    assert True


@pytest.mark.slow
def test_tagged():
    assert True


@pytest.mark.usefixtures("node_name")
def test_one_usefixtures_mark():
    assert True


@pytest.mark.usefixtures("class_scoped")
class TestGrouped:
    def test_one(self, package_scoped):
        assert package_scoped

    def test_two(self):
        assert True


class TestIndirect:
    @pytest.mark.parametrize("backend", ["mysql", "sqlite"], indirect=True)
    def test_it(self, backend):
        assert backend


class TestPlain:
    # The same method name as `TestIndirect`'s, which is what tells a per-class finding from a
    # per-module one.
    def test_it(self):
        assert True


class TestLifecycle:
    def setup_method(self, method):
        self.value = 1

    def teardown_method(self, method):
        self.value = None

    def test_uses_setup(self):
        assert self.value == 1


class TestLegacyUnit(unittest.TestCase):
    def setUp(self):
        self.value = 2

    def test_unittest_style(self):
        self.assertEqual(self.value, 2)
