"""Regression tests for velox._discovery (spec/03 §2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from velox._discovery import discover_files


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


# Review: symlink-loop protection is the one genuinely tricky thing in this module — an
# advertised feature (module docstring, spec/03 §2) with dedicated `(st_dev, st_ino)` code — and
# it has no test. `(tmp_path/"a").symlink_to(tmp_path)` plus a `discover_files([tmp_path])` that
# terminates is three lines; without it, deleting `visited` leaves this suite green and the
# runner hanging. Also untested: duplicate/overlapping roots (which currently double-collect,
# see the review note in `discover_files`) and an empty directory.
