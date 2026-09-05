"""Tests for `velox._cache`: the run cache's round trip, its tolerance of a file it cannot
read, and the merge rule that decides what one run is allowed to forget.
"""

from __future__ import annotations

import json
from pathlib import Path

from velox import _cache


def _cache_file(root: Path) -> Path:
    return root / _cache.CACHE_DIR_NAME / "lastfailed.json"


def test_load_without_a_cache_reads_as_nothing_recorded(tmp_path: Path) -> None:
    assert _cache.load(tmp_path) == _cache.NOTHING_RECORDED
    assert _cache.NOTHING_RECORDED.is_empty()


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    saved = _cache.LastRun(failed=("test_a.py::test_x",), error_files=("test_b.py",))
    _cache.save(tmp_path, saved)
    assert _cache.load(tmp_path) == saved


def test_save_writes_a_gitignore_covering_the_cache_directory(tmp_path: Path) -> None:
    """A project must not pick up a diff for having run velox once."""
    _cache.save(tmp_path, _cache.LastRun(failed=("test_a.py::test_x",)))
    assert (tmp_path / _cache.CACHE_DIR_NAME / ".gitignore").read_text().endswith("*\n")


def test_save_leaves_an_existing_gitignore_alone(tmp_path: Path) -> None:
    directory = tmp_path / _cache.CACHE_DIR_NAME
    directory.mkdir()
    (directory / ".gitignore").write_text("# mine\n")
    _cache.save(tmp_path, _cache.LastRun(failed=("test_a.py::test_x",)))
    assert (directory / ".gitignore").read_text() == "# mine\n"


def test_save_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    _cache.save(tmp_path, _cache.LastRun(failed=("test_a.py::test_x",)))
    directory = tmp_path / _cache.CACHE_DIR_NAME
    assert sorted(entry.name for entry in directory.iterdir()) == [".gitignore", "lastfailed.json"]


def test_load_of_a_corrupt_cache_reads_as_nothing_recorded(tmp_path: Path) -> None:
    """Hand-edited, half-written or truncated -- none of it may take a run down."""
    path = _cache_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json at all")
    assert _cache.load(tmp_path) == _cache.NOTHING_RECORDED


def test_load_of_another_schema_version_reads_as_nothing_recorded(tmp_path: Path) -> None:
    path = _cache_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 99, "failed": ["test_a.py::test_x"]}))
    assert _cache.load(tmp_path) == _cache.NOTHING_RECORDED


def test_load_drops_entries_that_are_not_strings(tmp_path: Path) -> None:
    """The cache is a file a user can edit, so every field is read as untrusted."""
    path = _cache_file(tmp_path)
    path.parent.mkdir(parents=True)
    payload = {"version": 1, "failed": ["test_a.py::test_x", 7], "error_files": 3}
    path.write_text(json.dumps(payload))
    assert _cache.load(tmp_path) == _cache.LastRun(failed=("test_a.py::test_x",))


def test_save_over_an_unwritable_directory_is_silent(tmp_path: Path) -> None:
    """The cache is an optimization; a read-only tree costs the next --lf its ordering and
    nothing else."""
    (tmp_path / _cache.CACHE_DIR_NAME).write_text("not a directory")
    _cache.save(tmp_path, _cache.LastRun(failed=("test_a.py::test_x",)))
    assert _cache.load(tmp_path) == _cache.NOTHING_RECORDED


def test_merge_replaces_what_this_run_settled() -> None:
    previous = _cache.LastRun(failed=("test_a.py::test_x", "test_a.py::test_y"))
    merged = _cache.merge(
        previous,
        failed=["test_a.py::test_y"],
        errored=[],
        settled_ids={"test_a.py::test_x", "test_a.py::test_y"},
        settled_files={"test_a.py"},
    )
    assert merged == _cache.LastRun(failed=("test_a.py::test_y",))


def test_merge_keeps_failures_this_run_never_reached() -> None:
    """What a `--maxfail` stop, a Ctrl-C, or a run over one directory leaves untouched."""
    previous = _cache.LastRun(failed=("test_a.py::test_x", "test_b.py::test_y"))
    merged = _cache.merge(
        previous,
        failed=[],
        errored=[],
        settled_ids={"test_a.py::test_x"},
        settled_files={"test_a.py"},
    )
    assert merged == _cache.LastRun(failed=("test_b.py::test_y",))


def test_merge_forgets_a_file_that_collects_again() -> None:
    previous = _cache.LastRun(error_files=("test_a.py", "test_b.py"))
    merged = _cache.merge(
        previous, failed=[], errored=[], settled_ids=set(), settled_files={"test_a.py"}
    )
    assert merged == _cache.LastRun(error_files=("test_b.py",))


def test_merge_orders_its_output_so_the_file_is_stable() -> None:
    merged = _cache.merge(
        _cache.NOTHING_RECORDED,
        failed=["test_b.py::test_y", "test_a.py::test_x"],
        errored=["z.py", "a.py"],
        settled_ids=set(),
        settled_files=set(),
    )
    assert merged.failed == ("test_a.py::test_x", "test_b.py::test_y")
    assert merged.error_files == ("a.py", "z.py")
