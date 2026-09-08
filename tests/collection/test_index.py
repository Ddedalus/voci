"""Tests for `voci._collection.index`: what a real collection is allowed to enter into it, and
what a later `--collect-only` is allowed to read back out of it without importing anything.
"""

from __future__ import annotations

import json
from pathlib import Path

from _support import make_record

from voci._cache import CACHE_DIR_NAME
from voci._collection import index
from voci._collection.collect import CollectionError, CollectionResult


def _stub() -> None:
    """Stands in for a collected test function; nothing here calls it."""


def _index_file(root: Path) -> Path:
    return root / CACHE_DIR_NAME / "collection.json"


def _entry_for(
    path: Path, *, ids: tuple[str, ...] = (), skipped: tuple[tuple[str, str], ...] = ()
) -> index.FileEntry:
    stat = path.stat()
    return index.FileEntry(
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        ids=ids,
        lines=tuple(1 for _ in ids),
        skipped=skipped,
    )


def test_load_without_an_index_reads_as_empty(tmp_path: Path) -> None:
    assert index.load(tmp_path) == index.EMPTY


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    saved = {
        "test_a.py": index.FileEntry(
            mtime_ns=1,
            size=2,
            ids=("test_a.py::test_x",),
            lines=(3,),
            skipped=(("test_a.py::test_y", "slow"),),
        )
    }
    index.save(tmp_path, saved)
    assert index.load(tmp_path) == saved


def test_load_of_a_corrupt_index_reads_as_empty(tmp_path: Path) -> None:
    path = _index_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json at all")
    assert index.load(tmp_path) == index.EMPTY


def test_load_of_another_schema_version_reads_as_empty(tmp_path: Path) -> None:
    path = _index_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 99, "files": {}}))
    assert index.load(tmp_path) == index.EMPTY


def test_load_drops_only_the_entry_that_is_malformed(tmp_path: Path) -> None:
    """The index is a file on disk a user can edit, so every entry is read as untrusted --
    one bad entry says nothing about the rest."""
    path = _index_file(tmp_path)
    path.parent.mkdir(parents=True)
    payload = {
        "version": 1,
        "files": {
            "test_a.py": {
                "mtime_ns": 1,
                "size": 2,
                "ids": ["test_a.py::test_x"],
                "lines": [1],
                "skipped": [],
            },
            "test_b.py": {
                "mtime_ns": "not an int",
                "size": 2,
                "ids": [],
                "lines": [],
                "skipped": [],
            },
        },
    }
    path.write_text(json.dumps(payload))
    assert set(index.load(tmp_path)) == {"test_a.py"}


def test_save_over_an_unwritable_directory_is_silent(tmp_path: Path) -> None:
    (tmp_path / CACHE_DIR_NAME).write_text("not a directory")
    index.save(tmp_path, {"test_a.py": _entry_for(tmp_path / CACHE_DIR_NAME)})
    assert index.load(tmp_path) == index.EMPTY


def test_answer_is_none_when_a_file_has_no_entry(tmp_path: Path) -> None:
    test_file = tmp_path / "test_a.py"
    test_file.write_text("")
    assert index.answer(index.EMPTY, [test_file], rootdir=tmp_path) is None


def test_answer_is_none_when_a_file_changed_since(tmp_path: Path) -> None:
    test_file = tmp_path / "test_a.py"
    test_file.write_text("one")
    entry = _entry_for(test_file, ids=("test_a.py::test_x",))
    test_file.write_text("one plus more")  # size changes -- the entry above is now stale.
    assert index.answer({"test_a.py": entry}, [test_file], rootdir=tmp_path) is None


def test_answer_is_none_when_only_some_files_are_fresh(tmp_path: Path) -> None:
    """All-or-nothing: one stale file among several is answered by a real collection, not by
    mixing a cached file in with a freshly imported one."""
    fresh = tmp_path / "test_a.py"
    fresh.write_text("")
    stale = tmp_path / "test_b.py"
    stale.write_text("one")
    entries = {"test_a.py": _entry_for(fresh), "test_b.py": _entry_for(stale)}
    stale.write_text("one plus more")
    assert index.answer(entries, [fresh, stale], rootdir=tmp_path) is None


def test_answer_reads_every_fresh_file_in_order(tmp_path: Path) -> None:
    file_a = tmp_path / "test_a.py"
    file_a.write_text("")
    file_b = tmp_path / "test_b.py"
    file_b.write_text("")
    entries = {
        "test_a.py": _entry_for(file_a, ids=("test_a.py::test_x",)),
        "test_b.py": _entry_for(file_b, skipped=(("test_b.py::test_y", "not ready"),)),
    }
    result = index.answer(entries, [file_a, file_b], rootdir=tmp_path)
    assert result == index.Answer(
        ids=("test_a.py::test_x",),
        skipped=(("test_b.py::test_y", "not ready"),),
        paths=("test_a.py",),
        lines=(1,),
        skipped_paths=("test_b.py",),
    )


def test_refresh_enters_a_freshly_collected_file(tmp_path: Path) -> None:
    test_file = tmp_path / "test_a.py"
    test_file.write_text("")
    result = CollectionResult(
        records=[make_record(0, _stub, "test_x", path=Path("test_a.py"))], errors=[], skipped=[]
    )
    updated = index.refresh(
        index.EMPTY,
        result,
        rootdir=tmp_path,
        files=[test_file],
        discovered=[test_file],
        roots=[tmp_path],
    )
    stat = test_file.stat()
    assert updated == {
        "test_a.py": index.FileEntry(
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            ids=("test_a.py::test_x",),
            lines=(1,),
            skipped=(),
        )
    }


def test_refresh_drops_a_file_that_errored(tmp_path: Path) -> None:
    test_file = tmp_path / "test_a.py"
    test_file.write_text("")
    previous = {"test_a.py": _entry_for(test_file, ids=("test_a.py::test_x",))}
    result = CollectionResult(
        records=[], errors=[CollectionError(path=Path("test_a.py"), message="boom")], skipped=[]
    )
    updated = index.refresh(
        previous,
        result,
        rootdir=tmp_path,
        files=[test_file],
        discovered=[test_file],
        roots=[tmp_path],
    )
    assert "test_a.py" not in updated


def test_refresh_drops_a_file_blocked_by_a_broken_package(tmp_path: Path) -> None:
    """The file itself raised no error of its own -- collect() never got that far -- but a
    stale success beside its package's own reported error would be misleading."""
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    test_file = package / "test_a.py"
    test_file.write_text("")
    previous = {"pkg/test_a.py": _entry_for(test_file, ids=("pkg/test_a.py::test_x",))}
    result = CollectionResult(
        records=[],
        errors=[CollectionError(path=Path("pkg/__init__.py"), message="boom")],
        skipped=[],
    )
    updated = index.refresh(
        previous,
        result,
        rootdir=tmp_path,
        files=[test_file],
        discovered=[test_file],
        roots=[tmp_path],
    )
    assert "pkg/test_a.py" not in updated


def test_refresh_drops_a_file_discovery_no_longer_produces_under_a_walked_root(
    tmp_path: Path,
) -> None:
    previous = {"test_gone.py": index.FileEntry(mtime_ns=1, size=1, ids=(), lines=(), skipped=())}
    result = CollectionResult(records=[], errors=[], skipped=[])
    updated = index.refresh(
        previous, result, rootdir=tmp_path, files=[], discovered=[], roots=[tmp_path]
    )
    assert updated == {}


def test_refresh_keeps_a_file_outside_every_walked_root(tmp_path: Path) -> None:
    """A run over one directory must not declare the rest of the suite's entries dead."""
    previous = {
        "elsewhere/test_a.py": index.FileEntry(mtime_ns=1, size=1, ids=(), lines=(), skipped=())
    }
    result = CollectionResult(records=[], errors=[], skipped=[])
    updated = index.refresh(
        previous, result, rootdir=tmp_path, files=[], discovered=[], roots=[tmp_path / "narrow"]
    )
    assert updated == previous


def test_refresh_keeps_a_file_an_lf_narrowing_left_untouched(tmp_path: Path) -> None:
    """`discovered` still names the file; `files` -- what `collect()` actually imported -- does
    not, exactly what `--lf` does to a file with nothing recorded against it."""
    untouched = tmp_path / "test_untouched.py"
    untouched.write_text("")
    previous = {"test_untouched.py": _entry_for(untouched, ids=("test_untouched.py::test_x",))}
    result = CollectionResult(records=[], errors=[], skipped=[])
    updated = index.refresh(
        previous, result, rootdir=tmp_path, files=[], discovered=[untouched], roots=[tmp_path]
    )
    assert updated == previous
