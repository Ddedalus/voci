"""Tests at the top of the suite, and the four spellings that reach them without being named.

Contributes the module-level autouse case (`top_stamp`, written in a test module, so its
declaration has to be written after the `def` it names), the whole-module `usefixtures` case (the
`pytestmark`), the single-test one and the class one — both of which widen to the module, since a
declaration covers a module and pytest's mark covered less than that.
"""

import pytest

pytestmark = pytest.mark.usefixtures("counted")


@pytest.fixture(autouse=True)
def top_stamp(ledger):
    ledger.append("top")


def test_the_autouse_fixtures_above_this_module_ran(ledger):
    assert "root" in ledger
    assert "top" in ledger


def test_the_modules_usefixtures_reaches_every_test(ledger):
    assert "counted" in ledger


@pytest.mark.usefixtures("tracked")
def test_one_test_names_a_fixture_it_never_reads(ledger):
    assert "tracked" in ledger


@pytest.mark.usefixtures("grouped")
class TestGroup:
    def test_a_class_names_a_fixture_for_its_methods(self, ledger):
        assert "grouped" in ledger

    def test_the_class_shares_the_modules_declarations(self, ledger):
        assert "counted" in ledger
