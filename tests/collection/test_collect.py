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
from velox._collection.tagexpr import compile_tag_expression


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


def test_a_class_test_method_becomes_a_collection_error(tmp_path: Path) -> None:
    """`class Test*` grouping isn't implemented (see `ROADMAP.md`); rather than collecting
    nothing, `collect` reports the shape as a `CollectionError` naming the class and its
    methods."""
    path = _write(
        tmp_path / "test_sample.py",
        "class TestSomething:\n    def test_method(self):\n        pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert result.errors[0].path == Path("test_sample.py")
    assert "TestSomething" in result.errors[0].message
    assert "test_method" in result.errors[0].message


def test_a_class_test_error_does_not_stop_module_level_tests_in_the_same_file(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "class TestSomething:\n"
        "    def test_method(self):\n"
        "        pass\n"
        "\n"
        "def test_ok():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert len(result.errors) == 1
    assert [record.qualname for record in result.records] == ["test_ok"]


def test_multiple_offending_classes_each_get_their_own_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "class TestA:\n"
        "    def test_a(self):\n"
        "        pass\n"
        "\n"
        "class TestB:\n"
        "    def test_b(self):\n"
        "        pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert {error.message.split(":", 1)[0] for error in result.errors} == {"TestA", "TestB"}


def test_a_class_named_like_a_test_with_no_test_methods_is_not_flagged(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "class TestHelper:\n    def helper(self):\n        pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert result.records == []


def test_a_staticmethod_test_method_is_flagged(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "class TestSomething:\n    @staticmethod\n    def test_method():\n        pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert "test_method" in result.errors[0].message


def test_a_classmethod_test_method_is_flagged(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "class TestSomething:\n    @classmethod\n    def test_method(cls):\n        pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert "test_method" in result.errors[0].message


def test_a_class_not_named_like_a_test_is_not_flagged_even_with_a_test_method(
    tmp_path: Path,
) -> None:
    """Only the `Test*` naming convention triggers the diagnostic; an ordinary helper class that
    happens to define a `test_*`-named method is left alone, same as before."""
    path = _write(
        tmp_path / "test_sample.py",
        "class Helper:\n    def test_method(self):\n        pass\n",
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


def test_tag_expr_excludes_non_matching_tests_into_deselected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "@velox.tag('slow')\n"
        "async def test_slow():\n"
        "    pass\n\n"
        "async def test_untagged():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path, tag_expr=compile_tag_expression("slow"))

    assert [record.qualname for record in result.records] == ["test_slow"]
    assert result.deselected == ["test_sample.py::test_untagged"]
    assert result.skipped == []
    assert result.errors == []


def test_skip_takes_priority_over_tag_expr_deselection(tmp_path: Path) -> None:
    """A skip-marked test is always `skipped`, never `deselected`, regardless of `-m` -- a
    test's skip status must not flip depending on which tags happen to be selected."""
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "@velox.tag('slow')\n"
        "@velox.skip('unrelated reason')\n"
        "async def test_slow_and_skipped():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path, tag_expr=compile_tag_expression("not slow"))

    assert result.records == []
    assert result.deselected == []
    assert [skipped.id for skipped in result.skipped] == ["test_sample.py::test_slow_and_skipped"]
    assert result.skipped[0].reason == "unrelated reason"


def test_no_tag_expr_deselects_nothing(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n@velox.tag('slow')\nasync def test_it():\n    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert [record.qualname for record in result.records] == ["test_it"]
    assert result.deselected == []


def test_deselected_tests_do_not_consume_an_index(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "async def test_a():\n"
        "    pass\n\n"
        "@velox.tag('slow')\n"
        "async def test_b():\n"
        "    pass\n\n"
        "async def test_c():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path, tag_expr=compile_tag_expression("not slow"))

    assert [record.qualname for record in result.records] == ["test_a", "test_c"]
    assert [record.index for record in result.records] == [0, 1]
    assert result.deselected == ["test_sample.py::test_b"]


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
    assert result.records[0].params is None


def test_a_parametrized_test_expands_into_one_record_per_case(tmp_path: Path) -> None:
    """The exact bug this closes: `n`/`expected` used to read as missing `Depends(...)`
    injections and fail collection outright."""
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "@velox.parametrize('n, expected', [(1, 2), (2, 4)])\n"
        "async def test_double(n, expected):\n"
        "    assert n * 2 == expected\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.id for record in result.records] == [
        "test_sample.py::test_double[1-2]",
        "test_sample.py::test_double[2-4]",
    ]
    assert [record.params for record in result.records] == [
        {"n": 1, "expected": 2},
        {"n": 2, "expected": 4},
    ]
    assert [record.index for record in result.records] == [0, 1]
    # Both cases share one function -> one resolution plan.
    assert result.records[0].plan is result.records[1].plan


def test_a_parametrized_test_shares_its_plan_with_an_actual_dependency(tmp_path: Path) -> None:
    """A parametrized argument and a `Depends(...)` injection on the same test coexist: the
    former becomes `params`, the latter is resolved through `plan` exactly as usual."""
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "@velox.fixture()\n"
        "async def db():\n"
        "    return 10\n\n"
        "@velox.parametrize('n', [1, 2])\n"
        "async def test_uses_both(n, value: int = velox.Depends(db)):\n"
        "    assert value == 10\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.params for record in result.records] == [{"n": 1}, {"n": 2}]
    for record in result.records:
        assert record.plan.steps[0].fixture.name == "db"


def test_stacked_parametrize_expands_the_full_cartesian_product(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "@velox.parametrize('outer', [1, 2])\n"
        "@velox.parametrize('inner', ['a', 'b'])\n"
        "async def test_grid(outer, inner):\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.id.rsplit("[", 1)[-1] for record in result.records] == [
        "1-a]",
        "1-b]",
        "2-a]",
        "2-b]",
    ]


def test_a_name_reused_across_stacked_parametrizes_is_a_collection_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "@velox.parametrize('n', [1, 2])\n"
        "@velox.parametrize('n', [3, 4])\n"
        "async def test_conflict(n):\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert "n" in result.errors[0].message


def test_a_parametrize_name_colliding_with_a_real_injection_is_a_collection_error(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "@velox.fixture()\n"
        "async def value():\n"
        "    return 1\n\n"
        "@velox.parametrize('value', [1, 2])\n"
        "async def test_conflict(value: int = velox.Depends(value)):\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert "value" in result.errors[0].message


def test_a_skipped_parametrized_test_is_reported_skipped_once_without_case_expansion(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "@velox.skip('not ready')\n"
        "@velox.parametrize('n', [1, 2])\n"
        "async def test_skipped(n):\n"
        "    raise AssertionError('must not run')\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert result.errors == []
    assert [skipped.id for skipped in result.skipped] == ["test_sample.py::test_skipped"]


def test_an_empty_argvalues_is_a_collection_error_not_a_silently_vanished_test(
    tmp_path: Path,
) -> None:
    """`@velox.parametrize` rejects an empty `argvalues` at decoration time (module-import time,
    from collection's point of view) rather than expanding into zero records with nothing to
    show for it."""
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n@velox.parametrize('n', [])\nasync def test_never_runs(n):\n    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert result.skipped == []
    assert len(result.errors) == 1
    assert "no argvalues" in result.errors[0].message


def test_a_parametrize_name_with_no_matching_parameter_is_a_collection_error(
    tmp_path: Path,
) -> None:
    """A typo'd `@velox.parametrize` argument name -- one that doesn't match any parameter of the
    test it decorates -- is refused at collection instead of expanding cleanly and then failing
    every case at call time with a bare `TypeError`."""
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n@velox.parametrize('typo', [1, 2])\nasync def test_x():\n    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert "typo" in result.errors[0].message


def test_a_parametrize_name_is_allowed_when_the_test_takes_star_kwargs(tmp_path: Path) -> None:
    """`**kwargs` absorbs any keyword, so a parametrize name with no same-named parameter is
    exactly as valid there as it would be calling the function by hand."""
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "@velox.parametrize('n', [1, 2])\n"
        "async def test_x(**kwargs):\n"
        "    assert kwargs['n'] in (1, 2)\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert len(result.records) == 2


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


def test_a_test_depending_on_a_parametrized_fixture_expands_into_one_record_per_case(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "@velox.fixture(params=['sqlite', 'postgres'])\n"
        "async def backend(param):\n"
        "    return param\n\n"
        "async def test_uses_backend(value: str = velox.Depends(backend)):\n"
        "    assert value in ('sqlite', 'postgres')\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.id for record in result.records] == [
        "test_sample.py::test_uses_backend[sqlite]",
        "test_sample.py::test_uses_backend[postgres]",
    ]
    # Each case gets its own specialized plan, not a shared one -- unlike @velox.parametrize.
    assert result.records[0].plan is not result.records[1].plan
    assert [record.params for record in result.records] == [None, None]


def test_a_parametrized_fixture_expands_a_transitive_dependent_too(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "@velox.fixture(params=['a', 'b'])\n"
        "async def backend(param):\n"
        "    return param\n\n"
        "@velox.fixture()\n"
        "async def engine(b: str = velox.Depends(backend)):\n"
        "    return f'engine+{b}'\n\n"
        "async def test_uses_engine(value: str = velox.Depends(engine)):\n"
        "    assert value.startswith('engine+')\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.id for record in result.records] == [
        "test_sample.py::test_uses_engine[a]",
        "test_sample.py::test_uses_engine[b]",
    ]


def test_fixture_params_and_test_level_parametrize_cross_multiply(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "@velox.fixture(params=['a', 'b'])\n"
        "async def backend(param):\n"
        "    return param\n\n"
        "@velox.parametrize('n', [1, 2])\n"
        "async def test_both(n, value: str = velox.Depends(backend)):\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.id.rsplit("[", 1)[-1] for record in result.records] == [
        "a-1]",
        "a-2]",
        "b-1]",
        "b-2]",
    ]
    assert [record.params for record in result.records] == [
        {"n": 1},
        {"n": 2},
        {"n": 1},
        {"n": 2},
    ]


def test_a_test_unrelated_to_a_parametrized_fixture_is_not_expanded(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "@velox.fixture(params=['a', 'b'])\n"
        "async def backend(param):\n"
        "    return param\n\n"
        "async def test_plain():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.id for record in result.records] == ["test_sample.py::test_plain"]


def test_a_fixture_parametrized_with_no_param_argument_is_a_collection_error(
    tmp_path: Path,
) -> None:
    """`@velox.fixture(params=...)` is validated at decoration time -- a broken fixture module
    surfaces as a whole-file `CollectionError`, the same way any other bad decoration would."""
    path = _write(
        tmp_path / "test_sample.py",
        "import velox\n\n"
        "@velox.fixture(params=['a', 'b'])\n"
        "async def backend():\n"
        "    return 1\n\n"
        "async def test_never_collected():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert "param" in result.errors[0].message


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
