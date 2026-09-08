"""Tests for voci_migrate.audit.completeness: what the dump names that no scan can find.

Every case mutates a checked-in corpus dump to name a fixture, test or class under a qualname its
real source file does not define — the shape marshmallow's four missed constructs actually had —
without touching the corpus source itself, so the suite stays the shared fixture every other audit
test reads too.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from voci_migrate import audit, model

CORPUS = Path(__file__).resolve().parents[1] / "corpus"
DUMPS = CORPUS / "dumps"


def _dump(suite: str) -> dict[str, Any]:
    text = (DUMPS / f"{suite}-pytest-9.1.json").read_text(encoding="utf-8")
    return json.loads(text)


def _audit(suite: str, dump: dict[str, Any]) -> audit.Audit:
    return audit.run(model.build(dump), root=CORPUS / suite)


def test_a_suite_the_matrix_was_written_against_has_nothing_unclassified() -> None:
    for suite in ("fixtures_showcase", "classes_showcase", "hazards_showcase"):
        result = _audit(suite, _dump(suite))
        assert result.unclassified == ()
        assert result.summary.unclassified_tests == 0


def test_a_fixture_the_scan_cannot_find_is_unclassified() -> None:
    suite = "fixtures_showcase"
    dump = _dump(suite)
    ghost = dump["fixture_defs"]["f0"]
    ghost["func"] = {**ghost["func"], "qualname": "ghost_fixture"}

    result = _audit(suite, dump)

    rows = [row for row in result.unclassified if row.kind == "fixture"]
    assert len(rows) == 1
    assert rows[0].name == ghost["argname"]
    assert rows[0].site.function == "ghost_fixture"


def test_a_test_the_scan_cannot_find_is_unclassified() -> None:
    suite = "fixtures_showcase"
    dump = _dump(suite)
    item = dump["items"][0]
    item["originalname"] = "test_ghost"
    item["nodeid"] = item["nodeid"].rsplit("::", 1)[0] + "::test_ghost"

    result = _audit(suite, dump)

    rows = [row for row in result.unclassified if row.kind == "test"]
    assert len(rows) == 1
    assert rows[0].name == "test_ghost"
    assert item["nodeid"] in rows[0].tests


def test_a_class_the_scan_cannot_find_is_unclassified() -> None:
    suite = "classes_showcase"
    dump = _dump(suite)
    renamed = [item for item in dump["items"] if item.get("cls") == "TestOverriding"]
    assert renamed
    for item in renamed:
        item["cls"] = "GhostClass"
        item["nodeid"] = item["nodeid"].replace("TestOverriding", "GhostClass")

    result = _audit(suite, dump)

    # One entry for the whole class, not one more per test method it no longer resolves to —
    # both would otherwise report the same root cause.
    assert len(result.unclassified) == 1
    row = result.unclassified[0]
    assert row.kind == "class"
    assert row.name == "GhostClass"
    assert set(row.tests) == {item["nodeid"] for item in renamed}


def test_an_unclassified_tests_own_test_is_pulled_out_of_the_clean_bucket() -> None:
    suite = "fixtures_showcase"
    before = _audit(suite, _dump(suite))
    dump = copy.deepcopy(_dump(suite))
    item = dump["items"][0]
    item["originalname"] = "test_ghost"
    item["nodeid"] = item["nodeid"].rsplit("::", 1)[0] + "::test_ghost"

    after = _audit(suite, dump)

    assert after.summary.clean_tests == before.summary.clean_tests - 1
    assert after.summary.unclassified_tests == 1
    assert after.summary.tests == before.summary.tests


def test_an_unreadable_source_file_draws_no_conclusion() -> None:
    # A fixture or test in a file `sources.scan` could not parse is already `unparsed`; this must
    # not additionally report it as unclassified, which would double-count the same gap.
    suite = "fixtures_showcase"
    dump = _dump(suite)
    ghost = dump["fixture_defs"]["f0"]
    ghost["func"] = {**ghost["func"], "file": "does_not_exist.py", "qualname": "ghost_fixture"}

    result = _audit(suite, dump)

    assert not any(row.name == ghost["argname"] for row in result.unclassified)
