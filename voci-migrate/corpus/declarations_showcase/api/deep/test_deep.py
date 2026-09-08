"""A test two directories below the declarations that reach it.

Contributes the chain case: this directory declares nothing of its own, so its `__init__.py` exists
only to keep the walk from this file up to the rootdir unbroken.
"""


def test_a_declaration_two_directories_up_still_reaches_here(ledger):
    assert "root" in ledger
    assert "api" in ledger
