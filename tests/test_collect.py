"""Regression tests for velox._collect (spec/03 §3-4)."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, cast

import pytest
from velox import _rewrite
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
    name = module_name_for(path, tmp_path)
    assert name.startswith("velox_tests.api_v2_")
    assert name.endswith(".test_a")


def test_module_name_for_two_same_named_files_never_collide(tmp_path: Path) -> None:
    """The entire content of pytest's `ImportPathMismatchError`, deleted rather than solved."""
    first = module_name_for(tmp_path / "pkg_a" / "test_utils.py", tmp_path)
    second = module_name_for(tmp_path / "pkg_b" / "test_utils.py", tmp_path)
    assert first != second


def test_module_name_for_escaping_does_not_reopen_the_collision_it_guards_against(
    tmp_path: Path,
) -> None:
    """A bare character-replace (`-` -> `_`) would make `api-v2` and `api_v2` collide again —
    exactly the collision `module_name_for` exists to prevent. The digest appended for whichever
    segment needed escaping keeps them apart."""
    dashed = module_name_for(tmp_path / "api-v2" / "test_a.py", tmp_path)
    clean = module_name_for(tmp_path / "api_v2" / "test_a.py", tmp_path)
    assert dashed != clean


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
    # `path` is relative to `rootdir` (spec/03 §1, I2) — not the absolute `tmp_path` the file
    # actually lives under, which would bake a machine-specific path into every id.
    assert result.records[0].id == "test_sample.py::test_b"
    assert result.records[0].path == Path("test_sample.py")


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
    assert result.errors[0].path == Path("test_broken.py")
    assert "boom" in result.errors[0].message
    assert "RuntimeError" in result.errors[0].message

    assert [record.qualname for record in result.records] == ["test_ok"]
    assert result.records[0].index == 0


def test_index_is_assigned_once_across_the_full_concatenation(tmp_path: Path) -> None:
    first = _write(tmp_path / "test_first.py", "async def test_one():\n    pass\n")
    second = _write(tmp_path / "test_second.py", "async def test_two():\n    pass\n")

    result = collect([first, second], rootdir=tmp_path)

    assert [record.index for record in result.records] == [0, 1]
    assert [record.path for record in result.records] == [
        Path("test_first.py"),
        Path("test_second.py"),
    ]


def test_a_failed_import_does_not_leave_a_half_initialized_module_in_sys_modules(
    tmp_path: Path,
) -> None:
    broken = _write(tmp_path / "test_broken.py", "raise RuntimeError('boom')\n")

    result = collect([broken], rootdir=tmp_path)

    assert len(result.errors) == 1
    assert module_name_for(broken, tmp_path) not in sys.modules


def test_a_successful_import_is_also_not_left_resident_in_sys_modules(tmp_path: Path) -> None:
    """Companion to the failure-path cleanup: leaving successful imports registered forever
    would mean every `collect()` call in a process permanently grows `sys.modules`."""
    fine = _write(tmp_path / "test_fine.py", "async def test_ok():\n    pass\n")

    result = collect([fine], rootdir=tmp_path)

    assert len(result.records) == 1
    assert module_name_for(fine, tmp_path) not in sys.modules


def test_helper_named_test_star_imported_from_elsewhere_is_not_collected_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spec/03 §4 step 1's `__module__` filter: a `test_*`-named function imported into a module
    from somewhere else must not be collected as if it were defined there."""
    # `test_main.py`'s `from ... import ...` below is an ordinary Python import statement, which
    # goes through the *real* import system (not velox's path-derived one) and therefore needs
    # the helper module findable on `sys.path` — velox itself never touches `sys.path` (spec/03
    # §3), this is purely to make the test's own fixture module importable the normal way.
    monkeypatch.syspath_prepend(str(tmp_path))
    helper = _write(
        tmp_path / "velox_test_collect_helper.py", "async def test_helper():\n    pass\n"
    )
    main = _write(
        tmp_path / "test_main.py",
        "from velox_test_collect_helper import test_helper\n\nasync def test_own():\n    pass\n",
    )

    try:
        result = collect([helper, main], rootdir=tmp_path)
    finally:
        # The plain `import` statement above registers a real, un-prefixed `sys.modules` entry
        # (distinct from velox's own `velox_tests.velox_test_collect_helper`, which `collect`
        # already cleans up itself) — this test's own doing, so this test's own cleanup.
        sys.modules.pop("velox_test_collect_helper", None)

    # `velox_test_collect_helper.py` doesn't match `test_*.py`/`*_test.py`, but it's collected
    # here as an explicit file to prove the point either way: `test_helper` is only ever
    # attributed to its own defining module, never to `test_main` too.
    ids = [record.id for record in result.records]
    assert ids == ["velox_test_collect_helper.py::test_helper", "test_main.py::test_own"]


def test_skip_marked_test_is_excluded_from_records_and_reported_skipped(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "@velox.skip('not ready')\n"
        "async def test_skipped():\n"
        "    raise AssertionError('must not run')\n\n"
        "async def test_runs():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert [record.qualname for record in result.records] == ["test_runs"]
    assert len(result.skipped) == 1
    assert result.skipped[0].id == "test_sample.py::test_skipped"
    assert result.skipped[0].reason == "not ready"
    assert result.errors == []


def test_truthy_skipif_excludes_a_test_falsy_skipif_does_not(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "@velox.skipif(True, reason='always')\n"
        "async def test_always_skipped():\n"
        "    pass\n\n"
        "@velox.skipif(False, reason='never')\n"
        "async def test_not_skipped():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert [record.qualname for record in result.records] == ["test_not_skipped"]
    assert [skipped.id for skipped in result.skipped] == ["test_sample.py::test_always_skipped"]


def test_depends_defaulted_parameter_is_a_collection_error_not_a_silent_pass(
    tmp_path: Path,
) -> None:
    """M0 has no DI — running a test whose parameter defaults to `Depends(...)` as-is would bind
    the test to the raw sentinel object and very likely still report `PASSED` (an I8 silent
    pass). Refusing it as a `CollectionError` instead makes the gap loud."""
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "@velox.fixture()\n"
        "async def db():\n"
        "    return 1\n\n"
        "async def test_needs_db(value: int = velox.Depends(db)):\n"
        "    assert value == 1\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert result.skipped == []
    assert len(result.errors) == 1
    assert "test_needs_db" in result.errors[0].message
    assert "Depends" in result.errors[0].message


def test_assertion_rewrite_hook_is_consulted_when_installed(tmp_path: Path) -> None:
    """End-to-end proof that `_import_module` actually gives the installed hook a chance
    (rather than bypassing `sys.meta_path` via a bare `spec_from_file_location`, the M0-skeleton
    bug this fixes): a rewritten module's failing `assert 2 == 3` carries the explanation text
    only the AST rewrite produces, not a bare `AssertionError`."""
    path = _write(
        tmp_path / "test_sample.py",
        "async def test_fails():\n    x = 2\n    y = 3\n    assert x == y\n",
    )

    setup = _rewrite.install([tmp_path], mode="rewrite", warn=False)
    try:
        assert not setup.degraded, setup.fallback_reason
        result = collect([path], rootdir=tmp_path)
    finally:
        _rewrite.uninstall()

    assert len(result.records) == 1
    failure = result.records[0].func
    try:
        # `func` is `Callable[..., object]` (see `_run.run_suite`'s identical cast) — only
        # `async def test_*` is ever collected, so this is always a coroutine at runtime.
        asyncio.run(cast("Coroutine[Any, Any, object]", failure()))
    except AssertionError as exc:
        assert "assert 2 == 3" in str(exc)
    else:
        raise AssertionError("expected the test function to fail")
