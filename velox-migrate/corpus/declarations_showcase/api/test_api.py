"""Tests under a directory that declares a fixture of its own.

Contributes the case where two package declarations reach one test — the rootdir's and this
directory's — which is what the chain of `__init__.py` files has to keep unbroken, and in the order
pytest set the fixtures up in.
"""


def test_both_directories_declarations_reach_this_test(ledger):
    assert "root" in ledger
    assert "api" in ledger


def test_the_outer_declaration_runs_first(ledger):
    assert ledger.index("root") < ledger.index("api")


def test_a_fixture_this_directory_defines_still_injects(client, ledger):
    assert client["ledger"] is ledger
