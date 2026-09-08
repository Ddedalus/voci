"""Tests for `voci._cache`: the run cache's round trip, its tolerance of a file it cannot
read, and the merge rule that decides what one run is allowed to forget.
"""

from __future__ import annotations

import json
from pathlib import Path

from voci import _cache


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
    """A project must not pick up a diff for having run voci once."""
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


def test_update_merges_into_what_is_on_disk_not_a_stale_baseline(tmp_path: Path) -> None:
    """Two runs sharing a rootdir must not erase each other's failures.

    A full run finds `b.py::t2` and writes; a scoped run that started earlier -- and so holds a
    baseline predating it -- finishes second with a whole payload of its own. Merging against the
    baseline loaded at startup would drop `b.py::t2`, leaving a cache that is non-empty and
    plausible with a real failure silently missing from it. Re-reading at the write keeps it:
    `b.py` is not among the scoped run's settled files, so it is carried forward by the same rule
    that already survives a `--maxfail` stop.
    """
    baseline = _cache.LastRun(failed=("a.py::t1",))
    _cache.save(tmp_path, baseline)

    # The full run: t1 now passes, t2 newly fails.
    _cache.update(
        tmp_path,
        baseline,
        failed=["b.py::t2"],
        errored=[],
        settled_ids={"a.py::t1", "b.py::t2"},
        settled_files={"a.py", "b.py"},
    )
    # The scoped run, `voci a.py`, landing second off the older baseline.
    _cache.update(
        tmp_path,
        baseline,
        failed=["a.py::t1"],
        errored=[],
        settled_ids={"a.py::t1"},
        settled_files={"a.py"},
    )

    assert _cache.load(tmp_path).failed == ("a.py::t1", "b.py::t2")


def test_update_still_forgets_what_the_run_that_wrote_last_settled(tmp_path: Path) -> None:
    """Carrying entries forward must not resurrect one the writing run has an answer for: a run
    that collected `a.py` and saw `t1` pass is the freshest word on `t1`."""
    baseline = _cache.LastRun(failed=("a.py::t1", "b.py::t2"))
    _cache.save(tmp_path, baseline)

    _cache.update(
        tmp_path, baseline, failed=[], errored=[], settled_ids={"a.py::t1"}, settled_files={"a.py"}
    )

    assert _cache.load(tmp_path).failed == ("b.py::t2",)


def test_update_falls_back_to_the_startup_baseline_when_the_re_read_fails(tmp_path: Path) -> None:
    """A cache that has gone unreadable since startup must not read as "nothing recorded".

    `load` flattens an unreadable file into `NOTHING_RECORDED`, which is the right answer for a
    caller asking what the last run found and the wrong one to merge into: it would drop every
    failure this run never reached, the exact loss the re-read exists to prevent. The startup
    baseline is stale but real.
    """
    baseline = _cache.LastRun(failed=("a.py::t1", "b.py::t2"))
    _cache.save(tmp_path, baseline)
    _cache_file(tmp_path).write_text("{ truncated")

    _cache.update(
        tmp_path, baseline, failed=[], errored=[], settled_ids={"a.py::t1"}, settled_files={"a.py"}
    )

    # t1, settled and passing, is dropped; t2, which this run never reached, survives.
    assert _cache.load(tmp_path).failed == ("b.py::t2",)
