"""Tests for `velox._collection.lastfailed`: which files `--lf` bothers importing, and what
`--lf`/`--ff` do to the records collection produced.
"""

from __future__ import annotations

from pathlib import Path

from _support import make_record

from velox._cache import NOTHING_RECORDED, LastRun
from velox._collection import lastfailed
from velox._collection.collect import CollectionError, CollectionResult, Skipped


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


def _skip(path: str, qualname: str) -> Skipped:
    return Skipped(id=f"{path}::{qualname}", reason="later", path=Path(path))


def test_candidate_files_keeps_the_tree_under_a_package_that_failed_to_import(
    tmp_path: Path,
) -> None:
    """Discovery never yields an `__init__.py`, so matching it exactly would match nothing."""
    files = [tmp_path / "pkg" / "test_a.py", tmp_path / "test_b.py"]
    last_run = LastRun(error_files=("pkg/__init__.py",))
    kept = lastfailed.candidate_files(files, last_run, rootdir=tmp_path)
    assert kept == [tmp_path / "pkg" / "test_a.py"]


def test_select_deselects_a_skip_that_is_not_a_recorded_failure() -> None:
    """Left in, it would report a --lf that executed nothing as a run that collected
    something."""
    collected = _collected(("test_a.py", "test_x"))
    collected.skipped.append(_skip("test_a.py", "test_skipped"))
    selected = lastfailed.select(collected, LastRun(failed=("test_a.py::test_x",)))
    assert selected.skipped == []
    assert selected.deselected == ["test_a.py::test_skipped"]


def test_select_keeps_a_skip_that_is_a_recorded_failure() -> None:
    collected = _collected()
    collected.skipped.append(_skip("test_a.py", "test_x"))
    selected = lastfailed.select(collected, LastRun(failed=("test_a.py::test_x",)))
    assert [skip.id for skip in selected.skipped] == ["test_a.py::test_x"]


def test_vanished_names_an_id_the_file_no_longer_holds() -> None:
    collected = _collected(("test_a.py", "test_new"))
    last_run = LastRun(failed=("test_a.py::test_old",))
    assert lastfailed.vanished(last_run, collected, known_files={"test_a.py"}) == {
        "test_a.py::test_old"
    }


def test_vanished_leaves_an_id_in_a_file_this_run_did_not_read() -> None:
    collected = _collected(("test_a.py", "test_x"))
    last_run = LastRun(failed=("test_b.py::test_y",))
    assert lastfailed.vanished(last_run, collected, known_files={"test_a.py"}) == set()


def test_vanished_leaves_an_id_that_was_only_deselected() -> None:
    collected = _collected()
    collected.deselected.append("test_a.py::test_x")
    last_run = LastRun(failed=("test_a.py::test_x",))
    assert lastfailed.vanished(last_run, collected, known_files={"test_a.py"}) == set()


def test_vanished_leaves_a_case_of_a_test_deselected_before_expansion() -> None:
    """`-m` excludes a test before its cases exist, so the recorded case id has only the bare
    id to match against."""
    collected = _collected()
    collected.deselected.append("test_a.py::test_x")
    collected.unexpanded.append("test_a.py::test_x")
    last_run = LastRun(failed=("test_a.py::test_x[one]",))
    assert lastfailed.vanished(last_run, collected, known_files={"test_a.py"}) == set()


def test_vanished_names_a_case_of_a_test_that_is_now_skipped() -> None:
    """A skip settles the test's cases; matching them to the bare id would leave them recorded
    for good, since nothing is ever going to run one again."""
    collected = _collected()
    collected.skipped.append(_skip("test_a.py", "test_x"))
    collected.unexpanded.append("test_a.py::test_x")
    last_run = LastRun(failed=("test_a.py::test_x[one]",))
    assert lastfailed.vanished(last_run, collected, known_files={"test_a.py"}) == {
        "test_a.py::test_x[one]"
    }


def test_vanished_names_an_id_whose_file_this_run_knows_is_gone() -> None:
    """A deleted file is handed to `vanished` through `known_files` the same way an imported one
    is: its test set is empty, so every id recorded under it is one nothing will run again."""
    collected = _collected()
    last_run = LastRun(failed=("test_gone.py::test_x",))
    assert lastfailed.vanished(last_run, collected, known_files={"test_gone.py"}) == {
        "test_gone.py::test_x"
    }


def test_missing_paths_names_a_recorded_file_that_no_longer_exists(tmp_path: Path) -> None:
    (tmp_path / "test_a.py").write_text("")
    last_run = LastRun(failed=("test_a.py::test_x", "test_gone.py::test_y"))
    assert lastfailed.missing_paths(last_run, rootdir=tmp_path) == {"test_gone.py"}


def test_missing_paths_names_a_recorded_error_file_that_no_longer_exists(tmp_path: Path) -> None:
    """`error_files` goes the same way as `failed`: a file deleted while it was still failing to
    import has nothing left to import, and no later run would take it out."""
    last_run = LastRun(error_files=("pkg/__init__.py",))
    assert lastfailed.missing_paths(last_run, rootdir=tmp_path) == {"pkg/__init__.py"}


def test_missing_paths_is_empty_when_every_recorded_file_is_still_there(tmp_path: Path) -> None:
    (tmp_path / "test_a.py").write_text("")
    last_run = LastRun(failed=("test_a.py::test_x",), error_files=("test_a.py",))
    assert lastfailed.missing_paths(last_run, rootdir=tmp_path) == set()


def _package(tmp_path: Path, *relpaths: str) -> None:
    for relpath in relpaths:
        target = tmp_path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("")


def test_settled_paths_answers_for_a_package_whose_tree_was_collected(tmp_path: Path) -> None:
    _package(tmp_path, "pkg/__init__.py", "pkg/test_a.py")
    assert lastfailed.settled_paths({"pkg/test_a.py"}, rootdir=tmp_path) == {
        "pkg/test_a.py",
        "pkg/__init__.py",
    }


def test_settled_paths_leaves_a_package_no_file_was_collected_under(tmp_path: Path) -> None:
    _package(tmp_path, "pkg/__init__.py", "test_b.py")
    assert lastfailed.settled_paths({"test_b.py"}, rootdir=tmp_path) == {"test_b.py"}


def test_settled_paths_stops_at_a_directory_without_an_init(tmp_path: Path) -> None:
    """Collection imports a package by walking an unbroken `__init__.py` chain up from the file.
    A namespace directory ends that walk, so `velox pkg/sub` never imports `pkg/__init__.py` and
    has no business clearing a recorded failure to import it."""
    _package(tmp_path, "pkg/__init__.py", "pkg/sub/test_a.py")
    assert lastfailed.settled_paths({"pkg/sub/test_a.py"}, rootdir=tmp_path) == {
        "pkg/sub/test_a.py"
    }


def test_emptied_packages_settles_a_package_with_no_test_left_under_it(tmp_path: Path) -> None:
    last_run = LastRun(error_files=("pkg/__init__.py",))
    assert lastfailed.emptied_packages(
        last_run, discovered={"test_b.py"}, roots=[tmp_path], rootdir=tmp_path
    ) == {"pkg/__init__.py"}


def test_emptied_packages_leaves_a_package_that_still_holds_a_test(tmp_path: Path) -> None:
    last_run = LastRun(error_files=("pkg/__init__.py",))
    assert (
        lastfailed.emptied_packages(
            last_run, discovered={"pkg/test_a.py"}, roots=[tmp_path], rootdir=tmp_path
        )
        == set()
    )


def test_emptied_packages_leaves_a_package_only_partly_walked(tmp_path: Path) -> None:
    """`velox pkg/sub` looked inside the package, not at it: what the rest of `pkg/` holds is
    exactly what this run did not find out."""
    last_run = LastRun(error_files=("pkg/__init__.py",))
    assert (
        lastfailed.emptied_packages(
            last_run,
            discovered=set(),
            roots=[tmp_path / "pkg" / "sub"],
            rootdir=tmp_path,
        )
        == set()
    )


def test_emptied_packages_leaves_a_package_outside_the_roots_walked(tmp_path: Path) -> None:
    """`velox one/` looked in one directory and must not conclude anything about another."""
    last_run = LastRun(error_files=("two/pkg/__init__.py",))
    assert (
        lastfailed.emptied_packages(
            last_run, discovered={"one/test_a.py"}, roots=[tmp_path / "one"], rootdir=tmp_path
        )
        == set()
    )


def test_error_paths_keeps_an_error_on_a_path_the_run_answers_for() -> None:
    error = CollectionError(path=Path("test_a.py"), message="boom")
    assert lastfailed.error_paths([error], answered={"test_a.py"}) == {"test_a.py"}


def test_error_paths_drops_an_error_on_a_module_velox_never_collects() -> None:
    """`_misplaced_declarations` reports a helper module under `rootdir` that discovery does not
    hand back, so nothing would ever settle the entry."""
    error = CollectionError(path=Path("helper.py"), message="boom")
    assert lastfailed.error_paths([error], answered={"test_a.py"}) == set()


def test_error_paths_drops_an_absolute_path(tmp_path: Path) -> None:
    """A misplaced declaration outside `rootdir` is named absolutely -- machine-specific, and
    never equal to a rootdir-relative discovery path."""
    error = CollectionError(path=tmp_path / "helper.py", message="boom")
    assert lastfailed.error_paths([error], answered={"test_a.py"}) == set()


def test_error_paths_drops_a_dotted_module_name() -> None:
    """A module with no `__file__` is named by its dotted name, which is not a path at all."""
    error = CollectionError(path=Path("some.module"), message="boom")
    assert lastfailed.error_paths([error], answered={"test_a.py"}) == set()


def test_read_files_drops_a_file_under_a_package_that_failed_to_import() -> None:
    """The file carries no error of its own -- the `__init__.py` does -- but it was never read,
    so it says nothing about which of its tests still exist."""
    assert lastfailed.read_files({"pkg/test_a.py", "test_b.py"}, {"pkg/__init__.py"}) == {
        "test_b.py"
    }


def test_read_files_drops_a_file_that_failed_to_import_itself() -> None:
    assert lastfailed.read_files({"test_a.py", "test_b.py"}, {"test_a.py"}) == {"test_b.py"}
