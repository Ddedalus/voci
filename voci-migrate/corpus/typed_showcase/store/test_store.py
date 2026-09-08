"""Tests one directory down, reading this directory's fixtures and the root's together.

Contributes the cross-directory case for both kinds of type: one named only under
`TYPE_CHECKING` where it was written, and one written in a `conftest.py` that moves.
"""


def test_report_names_a_type_checking_only_type(report):
    assert report.rows == 1


def test_ledger_names_a_type_its_conftest_writes(ledger):
    ledger.entries.append("entry")

    assert ledger.entries == ["entry"]


def test_a_root_fixture_reaches_here_too(session, ledger):
    assert session.dsn == "sqlite:///showcase"
    assert ledger.entries == []
