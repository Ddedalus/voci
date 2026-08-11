"""Tests for velox._collection.collect: module import and collection errors."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, cast

from _support import Project

import pytest
from velox._assertions import rewrite as _rewrite
from velox._collection.collect import collect, module_name_for


def _write(path: Path, source: str) -> Path:
    return Project(path.parent).write(path.name, source)


def test_module_name_for_is_path_derived_and_rootdir_relative(tmp_path: Path) -> None:
    path = tmp_path / "pkg" / "test_utils.py"
    assert module_name_for(path, tmp_path) == "velox_tests.pkg.test_utils"


def test_module_name_for_escapes_non_identifier_segments(tmp_path: Path) -> None:
    path = tmp_path / "api-v2" / "test_a.py"
    name = module_name_for(path, tmp_path)
    assert name.startswith("velox_tests.api_v2_")
    assert name.endswith(".test_a")


def test_module_name_for_two_same_named_files_never_collide(tmp_path: Path) -> None:
    first = module_name_for(tmp_path / "pkg_a" / "test_utils.py", tmp_path)
    second = module_name_for(tmp_path / "pkg_b" / "test_utils.py", tmp_path)
    assert first != second


def test_module_name_for_escaping_does_not_reopen_the_collision_it_guards_against(
    tmp_path: Path,
) -> None:
    """A bare character-replace (`-` -> `_`) would make `api-v2` and `api_v2` collide again;
    the digest appended for whichever segment needed escaping keeps them apart."""
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
        "def test_sync():\n"  # sync `def test_*` — collected same as an async one
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.qualname for record in result.records] == ["test_b", "test_a", "test_sync"]
    assert [record.index for record in result.records] == [0, 1, 2]
    assert [record.lineno for record in result.records] == [1, 7, 10]
    # `path` is relative to `rootdir` -- not the absolute `tmp_path` the file actually lives
    # under, which would bake a machine-specific path into every id.
    assert result.records[0].id == "test_sample.py::test_b"
    assert result.records[0].path == Path("test_sample.py")


def test_sync_def_test_star_is_collected_on_its_own(tmp_path: Path) -> None:
    path = _write(tmp_path / "test_sample.py", "def test_sync():\n    pass\n")

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.qualname for record in result.records] == ["test_sync"]


def test_a_class_is_not_collected_even_when_named_like_a_test(tmp_path: Path) -> None:
    """`class Test*` grouping isn't implemented (see `ROADMAP.md`); `_is_own_test_function`
    excludes it because a class isn't a `FunctionType`, not because of its name."""
    path = _write(
        tmp_path / "test_sample.py",
        "class TestSomething:\n    def test_method(self):\n        pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert result.records == []


def test_import_error_becomes_a_collection_error_and_does_not_abort(tmp_path: Path) -> None:
    broken = _write(tmp_path / "test_broken.py", "raise RuntimeError('boom')\n")
    fine = _write(
        tmp_path / "test_fine.py",
        "async def test_ok():\n    pass\n",
    )

    # The broken file is first: a failure must not stop collection of files after it.
    result = collect([broken, fine], rootdir=tmp_path)

    assert len(result.errors) == 1
    assert result.errors[0].path == Path("test_broken.py")
    assert "boom" in result.errors[0].message
    assert "RuntimeError" in result.errors[0].message

    assert [record.qualname for record in result.records] == ["test_ok"]
    assert result.records[0].index == 0


def test_a_spec_that_no_loader_can_be_built_for_becomes_a_collection_error(
    tmp_path: Path,
) -> None:
    """A path `spec_from_file_location` can't map to a loader (no rewrite hook installed, and
    no recognized suffix) surfaces as a `CollectionError`, not an uncaught `ImportError`."""
    path = _write(tmp_path / "test_sample.weird", "x = 1\n")

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert result.errors[0].path == Path("test_sample.weird")
    assert "cannot build an import spec" in result.errors[0].message


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
    """A successful import is also removed from `sys.modules` after collection."""
    fine = _write(tmp_path / "test_fine.py", "async def test_ok():\n    pass\n")

    result = collect([fine], rootdir=tmp_path)

    assert len(result.records) == 1
    assert module_name_for(fine, tmp_path) not in sys.modules


def test_helper_named_test_star_imported_from_elsewhere_is_not_collected_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `__module__` filter: a `test_*`-named function imported into a module from
    somewhere else must not be collected as if it were defined there."""
    # `test_main.py`'s `from ... import ...` below is an ordinary Python import statement, which
    # goes through the *real* import system (not velox's path-derived one) and therefore needs
    # the helper module findable on `sys.path` — velox itself never touches `sys.path`, this is
    # purely to make the test's own fixture module importable the normal way.
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
        # The plain `import` statement above registers a real, un-prefixed `sys.modules` entry,
        # distinct from velox's own `velox_tests.velox_test_collect_helper`.
        sys.modules.pop("velox_test_collect_helper", None)

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


def test_depends_defaulted_parameter_is_collected_with_a_real_resolution_plan(
    tmp_path: Path,
) -> None:
    """A well-formed `Depends(...)` graph is not refused — collection resolves it via
    `_fixtures.plan_for` and attaches the resulting `ResolutionPlan` to the `TestRecord`."""
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

    assert result.errors == []
    assert result.skipped == []
    assert len(result.records) == 1
    record = result.records[0]
    assert len(record.plan.steps) == 1
    assert record.plan.steps[0].fixture.name == "db"
    assert record.plan.root_args == (("value", 0, False),)


def test_a_test_with_no_dependencies_still_gets_a_trivial_resolution_plan(
    tmp_path: Path,
) -> None:
    """Every `TestRecord` carries a `plan`, even an empty one — `_run.py` has no "does this test
    have fixtures" branch to keep in sync."""
    path = _write(tmp_path / "test_sample.py", "async def test_plain():\n    pass\n")

    result = collect([path], rootdir=tmp_path)

    assert len(result.records) == 1
    assert result.records[0].plan.steps == ()
    assert result.records[0].plan.root_args == ()


def test_a_malformed_di_graph_is_still_a_collection_error(tmp_path: Path) -> None:
    """A test whose fixture graph fails `plan_for`'s static validation (here: a session-scoped
    fixture depending on a function-scoped one) is refused exactly like a broken import —
    attributed to the file, collection of the rest of the suite continues."""
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "@velox.fixture()\n"
        "async def narrow():\n"
        "    return 1\n\n"
        "@velox.fixture(scope='session')\n"
        "async def wide(x: int = velox.Depends(narrow)):\n"
        "    return x\n\n"
        "async def test_needs_wide(value: int = velox.Depends(wide)):\n"
        "    assert value == 1\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert result.skipped == []
    assert len(result.errors) == 1
    assert "narrow" in result.errors[0].message
    assert "wide" in result.errors[0].message


def test_a_missing_injection_is_still_a_collection_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "async def test_needs_something(value: int):\n    assert value == 1\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert "test_needs_something" in result.errors[0].message
    assert "value" in result.errors[0].message


def test_assertion_rewrite_hook_is_consulted_when_installed(tmp_path: Path) -> None:
    """End-to-end proof that `_import_module` actually gives the installed hook a chance,
    rather than bypassing `sys.meta_path` via a bare `spec_from_file_location`: a rewritten
    module's failing `assert 2 == 3` carries the explanation text only the AST rewrite
    produces, not a bare `AssertionError`."""
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
        # `func` is `Callable[..., object]` (see `_run.run_suite`'s identical cast) — `test_fails`
        # above is declared `async def`, so this particular `failure()` is a coroutine.
        asyncio.run(cast("Coroutine[Any, Any, object]", failure()))
    except AssertionError as exc:
        assert "assert 2 == 3" in str(exc)
    else:
        raise AssertionError("expected the test function to fail")
