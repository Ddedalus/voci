"""Tests for velox._collection.discovery: file matching, ignore rules, deterministic ordering."""

from __future__ import annotations

from pathlib import Path

import pytest
from velox._collection.discovery import discover_files


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def test_finds_test_prefix_and_suffix_files(tmp_path: Path) -> None:
    _touch(tmp_path / "test_alpha.py")
    _touch(tmp_path / "beta_test.py")
    _touch(tmp_path / "not_collected.py")

    found = discover_files([tmp_path])

    assert found == sorted(found)
    names = {path.name for path in found}
    assert names == {"test_alpha.py", "beta_test.py"}


def test_ignores_default_ignore_dirs(tmp_path: Path) -> None:
    _touch(tmp_path / "test_visible.py")
    _touch(tmp_path / ".venv" / "test_hidden.py")
    _touch(tmp_path / "__pycache__" / "test_hidden.py")
    _touch(tmp_path / "node_modules" / "test_hidden.py")

    found = discover_files([tmp_path])

    assert [path.name for path in found] == ["test_visible.py"]


def test_explicit_file_passes_through_even_without_matching_pattern(tmp_path: Path) -> None:
    explicit = _touch(tmp_path / "conftest_helpers.py")

    found = discover_files([explicit])

    assert found == [explicit.resolve()]


def test_walk_is_deterministically_sorted(tmp_path: Path) -> None:
    _touch(tmp_path / "z_dir" / "test_z.py")
    _touch(tmp_path / "a_dir" / "test_a.py")
    _touch(tmp_path / "test_top.py")

    first = discover_files([tmp_path])
    second = discover_files([tmp_path])

    assert first == second
    # `a_dir` sorts before `test_top.py`, which sorts before `z_dir` at the top level.
    assert [path.relative_to(tmp_path) for path in first] == [
        Path("a_dir/test_a.py"),
        Path("test_top.py"),
        Path("z_dir/test_z.py"),
    ]


def test_returns_absolute_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _touch(tmp_path / "sub" / "test_rel.py")
    monkeypatch.chdir(tmp_path)

    found = discover_files([Path("sub")])

    assert found == [(tmp_path / "sub" / "test_rel.py").resolve()]
    assert all(path.is_absolute() for path in found)


def test_nonexistent_root_yields_nothing(tmp_path: Path) -> None:
    assert discover_files([tmp_path / "does_not_exist"]) == []


def test_empty_directory_yields_nothing(tmp_path: Path) -> None:
    assert discover_files([tmp_path]) == []


def test_symlink_loop_terminates_instead_of_recursing_forever(tmp_path: Path) -> None:
    """`visited` (`(st_dev, st_ino)`) stops a directory symlinked back into its own ancestry
    from recursing forever."""
    _touch(tmp_path / "test_top.py")
    loop = tmp_path / "loop"
    try:
        loop.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("platform/filesystem doesn't support directory symlinks")

    found = discover_files([tmp_path])

    assert [path.name for path in found] == ["test_top.py"]


def test_duplicate_roots_do_not_double_collect(tmp_path: Path) -> None:
    _touch(tmp_path / "test_alpha.py")

    found = discover_files([tmp_path, tmp_path])

    assert [path.name for path in found] == ["test_alpha.py"]


def test_overlapping_roots_do_not_double_collect(tmp_path: Path) -> None:
    _touch(tmp_path / "sub" / "test_nested.py")

    found = discover_files([tmp_path, tmp_path / "sub"])

    assert [path.name for path in found] == ["test_nested.py"]


def test_argument_order_does_not_change_the_result(tmp_path: Path) -> None:
    """`velox b a` and `velox a b` must discover the same tests in the same order."""
    _touch(tmp_path / "a" / "test_a.py")
    _touch(tmp_path / "b" / "test_b.py")

    forward = discover_files([tmp_path / "a", tmp_path / "b"])
    backward = discover_files([tmp_path / "b", tmp_path / "a"])

    assert forward == backward
