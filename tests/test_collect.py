"""Regression tests for velox._collect (spec/03 §3-4)."""

from __future__ import annotations

from pathlib import Path

from velox._collect import collect, module_name_for


def _write(path: Path, source: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return path


def test_module_name_for_is_path_derived_and_rootdir_relative(tmp_path: Path) -> None:
    path = tmp_path / "pkg" / "test_utils.py"
    assert module_name_for(path, tmp_path) == "velox_tests.pkg.test_utils"


def test_module_name_for_escapes_non_identifier_segments(tmp_path: Path) -> None:
    path = tmp_path / "api-v2" / "test_a.py"
    assert module_name_for(path, tmp_path) == "velox_tests.api_v2.test_a"


def test_module_name_for_two_same_named_files_never_collide(tmp_path: Path) -> None:
    """The entire content of pytest's `ImportPathMismatchError`, deleted rather than solved."""
    first = module_name_for(tmp_path / "pkg_a" / "test_utils.py", tmp_path)
    second = module_name_for(tmp_path / "pkg_b" / "test_utils.py", tmp_path)
    assert first != second


def test_collect_orders_records_by_definition_line_not_name(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "async def test_b():\n"
        "    pass\n"
        "\n"
        "def not_a_test():\n"
        "    pass\n"
        "\n"
        "async def test_a():\n"
        "    pass\n"
        "\n"
        "def test_sync():\n"  # sync `def test_*` — must not be collected
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.qualname for record in result.records] == ["test_b", "test_a"]
    assert [record.index for record in result.records] == [0, 1]
    assert [record.lineno for record in result.records] == [1, 7]
    assert result.records[0].id == f"{path}::test_b"
    assert result.records[0].path == path


def test_import_error_becomes_a_collection_error_and_does_not_abort(tmp_path: Path) -> None:
    broken = _write(tmp_path / "test_broken.py", "raise RuntimeError('boom')\n")
    fine = _write(
        tmp_path / "test_fine.py",
        "async def test_ok():\n    pass\n",
    )

    # Deterministic order matters here: the broken file first proves a failure doesn't stop
    # collection of files that come after it.
    result = collect([broken, fine], rootdir=tmp_path)

    assert len(result.errors) == 1
    assert result.errors[0].path == broken
    assert "boom" in result.errors[0].message
    assert "RuntimeError" in result.errors[0].message

    assert [record.qualname for record in result.records] == ["test_ok"]
    assert result.records[0].index == 0


# Review: two of `_collect`'s riskiest behaviours have no test at all.
# (1) `_import_module`'s `sys.modules.pop` on failure — the whole point of that `except
#     BaseException` is that a half-initialized module is not left behind for a later import to
#     find, and nothing asserts `module_name_for(broken, root) not in sys.modules`.
# (2) The `__module__` filter in `_is_own_test_function` — the "imported helper named `test_*`
#     is not collected twice" rule from spec/03 §4 step 1. A module doing
#     `from other import test_helper` would prove it; today the check could be deleted and the
#     suite would stay green.
# Worth adding alongside: a module importable only via the meta path, which would have caught
# the rewrite hook never being consulted.
def test_index_is_assigned_once_across_the_full_concatenation(tmp_path: Path) -> None:
    first = _write(tmp_path / "test_first.py", "async def test_one():\n    pass\n")
    second = _write(tmp_path / "test_second.py", "async def test_two():\n    pass\n")

    result = collect([first, second], rootdir=tmp_path)

    assert [record.index for record in result.records] == [0, 1]
    assert [record.path for record in result.records] == [first, second]
