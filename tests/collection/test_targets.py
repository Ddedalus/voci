"""Tests for velox._collection.targets: parsing `path.py::test_name` arguments and deciding
which collected tests they select."""

from __future__ import annotations

from pathlib import Path

import pytest
from velox._collection.targets import IdSelection, parse_target


def test_a_plain_path_has_no_selector() -> None:
    target = parse_target("tests/test_users.py")
    assert target.path == Path("tests/test_users.py")
    assert target.selector is None


def test_a_test_id_splits_at_the_first_double_colon() -> None:
    target = parse_target("tests/test_users.py::TestDelete::test_soft")
    assert target.path == Path("tests/test_users.py")
    assert target.selector == "TestDelete::test_soft"


def test_a_trailing_double_colon_yields_an_empty_selector() -> None:
    """Reported as a usage error by `cli.main`, not silently read as "the whole file"."""
    assert parse_target("tests/test_users.py::").selector == ""


def test_no_selectors_anywhere_means_no_selection_at_all() -> None:
    """The common case: nothing to filter, so collection skips id matching entirely."""
    assert IdSelection.of([parse_target("tests"), parse_target("tests/test_users.py")]) is None


@pytest.mark.parametrize(
    "selector, suffix, expected",
    [
        ("test_create", "test_create", True),
        ("test_create", "test_created", False),
        ("test_create", "test_other", False),
        # A function selector takes every case of a parametrized test with it.
        ("test_create", "test_create[admin]", True),
        # A case selector takes exactly that one.
        ("test_create[admin]", "test_create[admin]", True),
        ("test_create[admin]", "test_create[guest]", False),
        # A class selector takes every test inside it, nested groups included.
        ("TestDelete", "TestDelete::test_soft", True),
        ("TestDelete", "TestDelete::TestInner::test_deep", True),
        ("TestDelete", "TestDeleted::test_soft", False),
        ("TestDelete::test_soft", "TestDelete::test_soft", True),
    ],
)
def test_selects_matches_a_selector_against_an_id_tail(
    tmp_path: Path, selector: str, suffix: str, expected: bool
) -> None:
    path = tmp_path / "test_users.py"
    selection = IdSelection.of([parse_target(f"{path}::{selector}")])
    assert selection is not None
    assert selection.selects(path, suffix) is expected


def test_a_file_no_selector_names_is_unconstrained(tmp_path: Path) -> None:
    """`velox tests/ tests/test_users.py::test_create` runs all of tests/ -- only the file the
    selector names is narrowed."""
    selected = tmp_path / "test_users.py"
    selection = IdSelection.of([parse_target(f"{selected}::test_create")])
    assert selection is not None
    assert selection.selects(tmp_path / "test_orders.py", "test_anything") is True


def test_the_same_file_given_bare_and_with_a_selector_is_unconstrained(tmp_path: Path) -> None:
    """Asking for the whole file is the wider request, so it wins over a narrower one for the
    same file rather than the two silently intersecting to nothing."""
    path = tmp_path / "test_users.py"
    selection = IdSelection.of([parse_target(str(path)), parse_target(f"{path}::test_create")])
    assert selection is None


def test_two_selectors_on_one_file_are_a_union(tmp_path: Path) -> None:
    path = tmp_path / "test_users.py"
    selection = IdSelection.of(
        [parse_target(f"{path}::test_create"), parse_target(f"{path}::test_delete")]
    )
    assert selection is not None
    assert selection.selects(path, "test_create") is True
    assert selection.selects(path, "test_delete") is True
    assert selection.selects(path, "test_update") is False


def test_unmatched_names_the_selectors_that_matched_no_id(tmp_path: Path) -> None:
    """A selector matching nothing is a typo, and `cli.main` reports it as a usage error --
    ids are resolved against `rootdir`, which is what they are relative to."""
    path = tmp_path / "test_users.py"
    selection = IdSelection.of(
        [parse_target(f"{path}::test_create"), parse_target(f"{path}::test_typo")]
    )
    assert selection is not None
    assert selection.unmatched(["test_users.py::test_create"], rootdir=tmp_path) == ["test_typo"]


def test_unmatched_is_empty_when_every_selector_found_something(tmp_path: Path) -> None:
    path = tmp_path / "test_users.py"
    selection = IdSelection.of([parse_target(f"{path}::test_role")])
    assert selection is not None
    assert selection.unmatched(["test_users.py::test_role[admin]"], rootdir=tmp_path) == []
