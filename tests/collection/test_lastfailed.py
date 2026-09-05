"""Tests for `velox._collection.lastfailed`: which files `--lf` bothers importing, and what
`--lf`/`--ff` do to the records collection produced.
"""

from __future__ import annotations

from pathlib import Path

from _support import make_record

from velox._cache import NOTHING_RECORDED, LastRun
from velox._collection import lastfailed
from velox._collection.collect import CollectionResult


def _stub() -> None:
    """Stands in for a collected test function; nothing here calls it."""


def _collected(*qualnames_by_path: tuple[str, str]) -> CollectionResult:
    records = [
        make_record(index, _stub, qualname, path=Path(path))
        for index, (path, qualname) in enumerate(qualnames_by_path)
    ]
    return CollectionResult(records=records, errors=[], skipped=[])


def test_candidate_files_keeps_only_the_files_a_failure_names(tmp_path: Path) -> None:
    for name in ("test_a.py", "test_b.py"):
        (tmp_path / name).write_text("")
    files = [tmp_path / "test_a.py", tmp_path / "test_b.py"]
    last_run = LastRun(failed=("test_a.py::test_x",))
    assert lastfailed.candidate_files(files, last_run, rootdir=tmp_path) == [tmp_path / "test_a.py"]


def test_candidate_files_keeps_a_file_that_failed_to_collect(tmp_path: Path) -> None:
    """It contributed no ids to record, so the path is all there is to match on."""
    files = [tmp_path / "test_a.py", tmp_path / "test_b.py"]
    last_run = LastRun(error_files=("test_b.py",))
    assert lastfailed.candidate_files(files, last_run, rootdir=tmp_path) == [tmp_path / "test_b.py"]


def test_candidate_files_reads_the_path_up_to_the_first_separator(tmp_path: Path) -> None:
    """A qualname can hold `::` of its own (a test method on a `Test*` class), so the split
    that recovers the path has to be the first one, not the last."""
    files = [tmp_path / "test_a.py"]
    last_run = LastRun(failed=("test_a.py::TestGroup::test_x[a::b]",))
    assert lastfailed.candidate_files(files, last_run, rootdir=tmp_path) == files


def test_select_deselects_everything_that_did_not_fail() -> None:
    collected = _collected(("test_a.py", "test_x"), ("test_a.py", "test_y"))
    selected = lastfailed.select(collected, LastRun(failed=("test_a.py::test_y",)))
    assert [record.id for record in selected.records] == ["test_a.py::test_y"]
    assert selected.deselected == ["test_a.py::test_x"]


def test_select_keeps_every_test_of_a_file_that_failed_to_collect() -> None:
    """A run that would have reported the broken import must not quietly leave it out once the
    file imports again."""
    collected = _collected(("test_a.py", "test_x"), ("test_b.py", "test_y"))
    selected = lastfailed.select(collected, LastRun(error_files=("test_a.py",)))
    assert [record.id for record in selected.records] == ["test_a.py::test_x"]


def test_select_renumbers_what_it_keeps() -> None:
    """`index` is the position of a test in the run that is about to happen, so a selection
    that removed the tests in front of it leaves no gap."""
    collected = _collected(
        ("test_a.py", "test_x"), ("test_a.py", "test_y"), ("test_a.py", "test_z")
    )
    selected = lastfailed.select(collected, LastRun(failed=("test_a.py::test_z",)))
    assert [record.index for record in selected.records] == [0]


def test_select_appends_to_deselections_already_made() -> None:
    collected = _collected(("test_a.py", "test_x"))
    collected.deselected.append("test_a.py::test_earlier")
    selected = lastfailed.select(collected, LastRun(failed=("test_a.py::test_x",)))
    assert selected.deselected == ["test_a.py::test_earlier"]


def test_reorder_lifts_the_failures_to_the_front_and_keeps_the_rest() -> None:
    collected = _collected(
        ("test_a.py", "test_x"), ("test_b.py", "test_y"), ("test_c.py", "test_z")
    )
    reordered = lastfailed.reorder(collected, LastRun(failed=("test_c.py::test_z",)))
    assert [record.id for record in reordered.records] == [
        "test_c.py::test_z",
        "test_a.py::test_x",
        "test_b.py::test_y",
    ]
    assert [record.index for record in reordered.records] == [0, 1, 2]


def test_reorder_holds_each_half_in_collection_order() -> None:
    collected = _collected(
        ("test_a.py", "test_x"), ("test_b.py", "test_y"), ("test_c.py", "test_z")
    )
    last_run = LastRun(failed=("test_c.py::test_z", "test_a.py::test_x"))
    reordered = lastfailed.reorder(collected, last_run)
    assert [record.id for record in reordered.records] == [
        "test_a.py::test_x",
        "test_c.py::test_z",
        "test_b.py::test_y",
    ]


def test_reorder_with_nothing_recorded_changes_nothing() -> None:
    collected = _collected(("test_a.py", "test_x"), ("test_b.py", "test_y"))
    reordered = lastfailed.reorder(collected, NOTHING_RECORDED)
    assert [record.id for record in reordered.records] == [
        "test_a.py::test_x",
        "test_b.py::test_y",
    ]
