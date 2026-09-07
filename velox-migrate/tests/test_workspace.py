"""Tests for velox_migrate.workspace: the relocation branch, its rebase and export, and the
pre-conversion baseline snapshot.

`scaffold` is exercised against real git repositories -- there is no shortcut that checks the
rebase machinery without actually rebasing something.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _support import git, git_commit, git_repo

from velox_migrate import workspace


def _write(path: Path, name: str, content: str) -> None:
    (path / name).write_text(content, encoding="utf-8")


def test_scaffold_creates_the_relocation_branch_empty_at_the_tip(tmp_path: Path) -> None:
    source = git_repo(tmp_path / "source")
    _write(source, "test_thing.py", "def test_ok():\n    assert True\n")
    tip = git_commit(source, "initial")
    dest = tmp_path / "dest"

    result = workspace.scaffold(source, dest)

    assert result.created_branch is True
    assert result.tip == tip
    assert result.dest == dest.resolve()
    assert (dest / "test_thing.py").read_text(encoding="utf-8") == (
        "def test_ok():\n    assert True\n"
    )
    branch_tip = git(source, "rev-parse", workspace.RELOCATION_BRANCH).stdout.strip()
    assert branch_tip == tip


def test_scaffold_defaults_dest_to_sources_own_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = git_repo(tmp_path / "widget")
    _write(source, "a.txt", "x")
    git_commit(source, "initial")
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    result = workspace.scaffold(source)

    assert result.dest == (workdir / "widget").resolve()
    assert (result.dest / "a.txt").is_file()


def test_scaffold_reconnects_to_an_existing_relocation_branch_without_recreating_it(
    tmp_path: Path,
) -> None:
    source = git_repo(tmp_path / "source")
    _write(source, "a.py", "a = 1\n")
    git_commit(source, "initial")
    dest = tmp_path / "dest"
    first = workspace.scaffold(source, dest)
    assert first.created_branch is True

    second = workspace.scaffold(source, dest)

    assert second.created_branch is False
    assert second.tip == first.tip


def test_scaffold_picks_up_a_new_commit_on_source(tmp_path: Path) -> None:
    source = git_repo(tmp_path / "source")
    _write(source, "a.py", "a = 1\n")
    git_commit(source, "initial")
    dest = tmp_path / "dest"
    workspace.scaffold(source, dest)

    _write(source, "a.py", "a = 2\n")
    tip = git_commit(source, "prefactor")

    result = workspace.scaffold(source, dest)

    assert result.tip == tip
    assert (dest / "a.py").read_text(encoding="utf-8") == "a = 2\n"


def test_scaffold_removes_a_file_deleted_upstream(tmp_path: Path) -> None:
    source = git_repo(tmp_path / "source")
    _write(source, "a.py", "a = 1\n")
    _write(source, "b.py", "b = 1\n")
    git_commit(source, "initial")
    dest = tmp_path / "dest"
    workspace.scaffold(source, dest)
    assert (dest / "b.py").is_file()

    (source / "b.py").unlink()
    git_commit(source, "drop b")

    workspace.scaffold(source, dest)

    assert not (dest / "b.py").exists()


def test_scaffold_carries_a_relocation_fixup_across_a_later_source_commit(tmp_path: Path) -> None:
    source = git_repo(tmp_path / "source")
    _write(source, "conftest.py", "ROOT = '/orig/path'\n")
    _write(source, "test_thing.py", "def test_ok():\n    assert True\n")
    git_commit(source, "initial")
    dest = tmp_path / "dest"
    workspace.scaffold(source, dest)

    # A relocation fixup lands on the relocation branch, committed where scaffold checked it out
    # -- a worktree of its own, since the branch stays checked out there rather than in source's
    # own working directory.
    scratch = workspace._scratch_worktree(source.resolve())
    _write(scratch, "conftest.py", "ROOT = '/relocated/path'\n")
    git_commit(scratch, "relocation fixup")

    # A prefactor the suite keeps lands on source as an ordinary commit, on an unrelated file.
    _write(
        source,
        "test_thing.py",
        "def test_ok():\n    assert True\n\n\ndef test_more():\n    assert True\n",
    )
    git_commit(source, "prefactor")

    result = workspace.scaffold(source, dest)

    assert result.created_branch is False
    assert (dest / "conftest.py").read_text(encoding="utf-8") == "ROOT = '/relocated/path'\n"
    assert "test_more" in (dest / "test_thing.py").read_text(encoding="utf-8")


def test_scaffold_surfaces_a_real_conflict_for_the_user_to_resolve(tmp_path: Path) -> None:
    source = git_repo(tmp_path / "source")
    _write(source, "conftest.py", "ROOT = 'orig'\n")
    git_commit(source, "initial")
    dest = tmp_path / "dest"
    workspace.scaffold(source, dest)

    scratch = workspace._scratch_worktree(source.resolve())
    _write(scratch, "conftest.py", "ROOT = 'fixup'\n")
    git_commit(scratch, "relocation fixup")

    _write(source, "conftest.py", "ROOT = 'prefactor'\n")
    git_commit(source, "prefactor touches the same line")

    with pytest.raises(workspace.WorkspaceError, match="does not rebase cleanly"):
        workspace.scaffold(source, dest)

    # Left unresolved, a second attempt reports the stuck rebase rather than guessing at it.
    with pytest.raises(workspace.WorkspaceError, match="still unresolved"):
        workspace.scaffold(source, dest)


def test_scaffold_refuses_to_clobber_unrelated_content_at_dest(tmp_path: Path) -> None:
    source = git_repo(tmp_path / "source")
    _write(source, "a.py", "a = 1\n")
    git_commit(source, "initial")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "unrelated.txt").write_text("keep me\n", encoding="utf-8")

    with pytest.raises(workspace.WorkspaceError, match="already has content"):
        workspace.scaffold(source, dest)

    assert (dest / "unrelated.txt").read_text(encoding="utf-8") == "keep me\n"


def test_scaffold_refuses_a_non_git_source(tmp_path: Path) -> None:
    source = tmp_path / "plain"
    source.mkdir()

    with pytest.raises(workspace.WorkspaceError, match="not inside a git work tree"):
        workspace.scaffold(source, tmp_path / "dest")


def test_snapshot_baseline_copies_everything_but_tool_state(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _write(root, "a.py", "a = 1\n")
    package = root / "pkg"
    package.mkdir()
    (package / "b.py").write_text("b = 1\n", encoding="utf-8")
    tool_state = root / ".velox-migrate"
    tool_state.mkdir()
    (tool_state / "ground-truth.json").write_text("{}", encoding="utf-8")

    baseline = workspace.snapshot_baseline(root)

    assert baseline == root / workspace.BASELINE_DIR
    tree = baseline / workspace.TREE_DIR
    assert (tree / "a.py").read_text(encoding="utf-8") == "a = 1\n"
    assert (tree / "pkg" / "b.py").read_text(encoding="utf-8") == "b = 1\n"
    assert not (tree / ".velox-migrate").exists()
    # A copy, not a move: the suite's own sources still stand where they were.
    assert (root / "a.py").is_file()


def test_snapshot_baseline_replaces_an_earlier_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _write(root, "a.py", "first\n")
    workspace.snapshot_baseline(root)
    _write(root, "a.py", "second\n")
    _write(root, "b.py", "new\n")

    baseline = workspace.snapshot_baseline(root)

    tree = baseline / workspace.TREE_DIR
    assert (tree / "a.py").read_text(encoding="utf-8") == "second\n"
    assert (tree / "b.py").read_text(encoding="utf-8") == "new\n"
