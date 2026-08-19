"""Tests for velox_migrate.convert.edits: the diff a conversion shows and the tree it writes.

Diffs are asserted whole rather than searched for substrings, because the reason this renders
git's dialect at all is that someone reads it before agreeing to anything — and because a diff
that is stable to the byte is what lets a second conversion prove it changed nothing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from velox_migrate.convert.edits import Edit, EditSet

FIVE = "line 1\nline 2\nline 3\nline 4\nline 5\n"
EDITED = "line 1\nline 2\nchanged\nline 4\nline 5\n"


def test_each_kind_of_change_names_itself() -> None:
    assert Edit("a.py", "new", None).kind == "create"
    assert Edit("a.py", "new", "old").kind == "modify"
    assert Edit("a.py", None, "old").kind == "delete"
    assert Edit("fixtures.py", "new", "old", moved_from="conftest.py").kind == "move"


def test_an_edit_that_would_do_nothing_says_so() -> None:
    assert not Edit("a.py", FIVE, FIVE).changed
    assert not Edit("a.py", None, None).changed
    assert Edit("a.py", EDITED, FIVE).changed
    # The file relocates whether or not its text does.
    assert Edit("fixtures.py", FIVE, FIVE, moved_from="conftest.py").changed


def test_a_path_that_is_not_rootdir_relative_posix_is_refused() -> None:
    for path in ("/abs/a.py", "sub\\a.py", "../a.py", ""):
        with pytest.raises(ValueError, match="rootdir-relative"):
            Edit(path, "new", None)
    with pytest.raises(ValueError, match="rootdir-relative"):
        Edit("fixtures.py", "new", "old", moved_from="/abs/conftest.py")


def test_a_move_that_is_also_a_removal_is_refused() -> None:
    with pytest.raises(ValueError, match=r"fixtures\.py"):
        Edit("fixtures.py", None, "old", moved_from="conftest.py")
    with pytest.raises(ValueError, match="a move to its own path"):
        Edit("a.py", "new", "old", moved_from="a.py")


def test_two_edits_claiming_one_path_name_the_path() -> None:
    with pytest.raises(ValueError, match=r"Two edits claim tests/test_x\.py\."):
        EditSet((Edit("tests/test_x.py", "one", None), Edit("tests/test_x.py", "two", None)))


def test_two_moves_out_of_one_path_name_the_path() -> None:
    with pytest.raises(ValueError, match=r"Two edits move conftest\.py away\."):
        EditSet(
            (
                Edit("a/fixtures.py", "one", None, moved_from="conftest.py"),
                Edit("b/fixtures.py", "two", None, moved_from="conftest.py"),
            )
        )


def test_a_path_both_written_and_moved_away_names_the_path() -> None:
    # Writing a file and then removing it as a move's source is a lost file, whichever order
    # `apply` used, so it never gets that far.
    with pytest.raises(ValueError, match=r"conftest\.py is both written and moved away\."):
        EditSet(
            (
                Edit("conftest.py", "kept", "old"),
                Edit("fixtures.py", "moved", "old", moved_from="conftest.py"),
            )
        )


def test_the_edits_are_ordered_by_path_however_they_were_given() -> None:
    paths = ("z.py", "a/b.py", "a/a.py", "conftest.py")
    edits = EditSet(tuple(Edit(path, "new", None) for path in paths))

    assert [edit.path for edit in edits.edits] == ["a/a.py", "a/b.py", "conftest.py", "z.py"]


def test_a_modified_file_renders_as_git_renders_one() -> None:
    edits = EditSet((Edit("tests/test_x.py", EDITED, FIVE),))

    assert edits.diff() == (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -1,5 +1,5 @@\n"
        " line 1\n"
        " line 2\n"
        "-line 3\n"
        "+changed\n"
        " line 4\n"
        " line 5\n"
    )


def test_a_created_file_renders_against_dev_null() -> None:
    edits = EditSet((Edit("fixtures.py", "import velox\n", None),))

    assert edits.diff() == (
        "diff --git a/fixtures.py b/fixtures.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/fixtures.py\n"
        "@@ -0,0 +1 @@\n"
        "+import velox\n"
    )


def test_a_deleted_file_renders_against_dev_null() -> None:
    edits = EditSet((Edit("conftest.py", None, "import pytest\n"),))

    assert edits.diff() == (
        "diff --git a/conftest.py b/conftest.py\n"
        "deleted file mode 100644\n"
        "--- a/conftest.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-import pytest\n"
    )


def test_a_move_renders_as_a_rename_from_the_old_path() -> None:
    edits = EditSet((Edit("fixtures.py", EDITED, FIVE, moved_from="conftest.py"),))

    assert edits.diff() == (
        "diff --git a/conftest.py b/fixtures.py\n"
        "rename from conftest.py\n"
        "rename to fixtures.py\n"
        "--- a/conftest.py\n"
        "+++ b/fixtures.py\n"
        "@@ -1,5 +1,5 @@\n"
        " line 1\n"
        " line 2\n"
        "-line 3\n"
        "+changed\n"
        " line 4\n"
        " line 5\n"
    )


def test_a_move_of_unchanged_content_renders_as_the_rename_alone() -> None:
    edits = EditSet((Edit("fixtures.py", FIVE, FIVE, moved_from="conftest.py"),))

    assert edits.diff() == (
        "diff --git a/conftest.py b/fixtures.py\nrename from conftest.py\nrename to fixtures.py\n"
    )


def test_a_file_that_ends_without_a_newline_says_so_where_git_does() -> None:
    edits = EditSet((Edit("a.py", "one\ntwo", "one\n"),))

    assert edits.diff() == (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1,2 @@\n"
        " one\n"
        "+two\n"
        "\\ No newline at end of file\n"
    )


def test_only_the_files_that_change_appear() -> None:
    edits = EditSet(
        (
            Edit("changed.py", EDITED, FIVE),
            Edit("same.py", FIVE, FIVE),
            Edit("absent.py", None, None),
        )
    )

    assert [edit.path for edit in edits.changes] == ["changed.py"]
    assert edits.diff().count("diff --git") == 1


def test_a_conversion_that_changes_nothing_renders_nothing() -> None:
    # The whole phase's idempotency claim: converting an already-converted tree decides the same
    # text for every file, and that has to be visible as an empty diff rather than a rewrite.
    edits = EditSet((Edit("a.py", FIVE, FIVE), Edit("b/c.py", EDITED, EDITED)))

    assert edits.diff() == ""
    assert edits.changes == ()


def test_the_same_changes_render_the_same_bytes() -> None:
    given = (Edit("z.py", EDITED, FIVE), Edit("a.py", "new\n", None))

    assert EditSet(given).diff() == EditSet(tuple(reversed(given))).diff()


def test_applying_writes_every_file_and_reports_what_it_touched(tmp_path: Path) -> None:
    (tmp_path / "conftest.py").write_text(FIVE, encoding="utf-8")
    (tmp_path / "test_x.py").write_text(FIVE, encoding="utf-8")
    (tmp_path / "gone.py").write_text(FIVE, encoding="utf-8")
    edits = EditSet(
        (
            Edit("test_x.py", EDITED, FIVE),
            Edit("deep/nested/fixtures.py", EDITED, None),
            Edit("fixtures.py", FIVE, FIVE, moved_from="conftest.py"),
            Edit("gone.py", None, FIVE),
        )
    )

    touched = edits.apply(tmp_path)

    assert touched == (
        "conftest.py",
        "deep/nested/fixtures.py",
        "fixtures.py",
        "gone.py",
        "test_x.py",
    )
    assert (tmp_path / "test_x.py").read_text(encoding="utf-8") == EDITED
    assert (tmp_path / "deep/nested/fixtures.py").read_text(encoding="utf-8") == EDITED
    assert (tmp_path / "fixtures.py").read_text(encoding="utf-8") == FIVE
    assert not (tmp_path / "conftest.py").exists()
    assert not (tmp_path / "gone.py").exists()


def test_applying_reports_nothing_for_a_removal_of_a_file_that_is_not_there(tmp_path: Path) -> None:
    edits = EditSet((Edit("gone.py", None, FIVE),))

    assert edits.apply(tmp_path) == ()


def test_applying_leaves_the_files_that_do_not_change_alone(tmp_path: Path) -> None:
    kept = tmp_path / "a.py"
    kept.write_text(FIVE, encoding="utf-8")
    stamp = kept.stat().st_mtime_ns

    assert EditSet((Edit("a.py", FIVE, FIVE),)).apply(tmp_path) == ()
    assert kept.stat().st_mtime_ns == stamp


def test_a_second_conversion_of_the_applied_tree_is_a_no_op(tmp_path: Path) -> None:
    (tmp_path / "test_x.py").write_text(FIVE, encoding="utf-8")
    first = EditSet((Edit("test_x.py", EDITED, FIVE), Edit("fixtures.py", EDITED, None)))
    first.apply(tmp_path)

    # What a second run decides for each file, read back off the tree the first one wrote.
    again = EditSet(
        tuple(
            Edit(edit.path, edit.new_text, (tmp_path / edit.path).read_text(encoding="utf-8"))
            for edit in first.edits
        )
    )

    assert again.diff() == ""
    assert again.apply(tmp_path) == ()


def test_no_leftovers_are_written_beside_the_files(tmp_path: Path) -> None:
    EditSet((Edit("pkg/a.py", FIVE, None),)).apply(tmp_path)

    assert [path.name for path in (tmp_path / "pkg").iterdir()] == ["a.py"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is what reads this dialect")
def test_git_applies_the_rendered_diff(tmp_path: Path) -> None:
    # The claim is that the diff is git's, not merely diff-shaped: git itself is the only judge of
    # the rename, /dev/null and mode lines it carries.
    tree = tmp_path / "tree"
    tree.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=tree, check=True)
    (tree / "test_x.py").write_text(FIVE, encoding="utf-8")
    (tree / "conftest.py").write_text(FIVE, encoding="utf-8")
    (tree / "gone.py").write_text(FIVE, encoding="utf-8")
    edits = EditSet(
        (
            Edit("test_x.py", EDITED, FIVE),
            Edit("fixtures.py", EDITED, FIVE, moved_from="conftest.py"),
            Edit("new/added.py", FIVE, None),
            Edit("gone.py", None, FIVE),
        )
    )
    patch = tmp_path / "conversion.patch"
    patch.write_text(edits.diff(), encoding="utf-8")

    applied = subprocess.run(
        ["git", "apply", "-v", str(patch)], cwd=tree, capture_output=True, text=True, check=False
    )

    assert applied.returncode == 0, applied.stderr
    assert (tree / "fixtures.py").read_text(encoding="utf-8") == EDITED
    assert (tree / "new/added.py").read_text(encoding="utf-8") == FIVE
    assert not (tree / "conftest.py").exists()
    assert not (tree / "gone.py").exists()
